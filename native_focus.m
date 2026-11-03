// Exact-window recovery and private, constrained message-field entry for Call
// Desk. No key events, send clicks, or title-based window fallback are issued.
#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>
#import <Foundation/Foundation.h>
#import <libproc.h>
#import <sys/file.h>
#import <sys/stat.h>
#import <dlfcn.h>
#import <fcntl.h>
#import <time.h>
#import <unistd.h>
#import <poll.h>

typedef AXError (*WindowBridge)(AXUIElementRef, CGWindowID *);
static double deadline;
static BOOL activationDispatched = NO, raiseDispatched = NO;
static BOOL fieldSetDispatched = NO;
static BOOL fieldRequestMode = NO;
static NSMutableDictionary *diagnostic;

static void fail(NSString *code, NSString *message) {
    @throw [NSException exceptionWithName:code reason:message userInfo:nil];
}
static double now(void) {
    struct timespec value; clock_gettime(CLOCK_MONOTONIC, &value);
    return value.tv_sec + value.tv_nsec / 1e9;
}
static void withinBudget(void) {
    if (now() >= deadline) fail(@"TIMEOUT", @"Exact-window recovery deadline expired");
}
static void pauseBriefly(void) {
    withinBudget();
    // Pump AppKit activation notifications without issuing more actions.
    [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:MIN(0.05, deadline-now())]];
}
static void emit(NSDictionary *value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:nil];
    [[NSFileHandle fileHandleWithStandardOutput] writeData:data];
    [[NSFileHandle fileHandleWithStandardOutput] writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
}
static NSString *processToken(pid_t pid) {
    struct proc_bsdinfo info = {0};
    int count = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info));
    if (count != sizeof(info) || info.pbi_pid != (uint32_t)pid)
        fail(@"IDENTITY_UNAVAILABLE", @"Could not read the exact process generation");
    return [NSString stringWithFormat:@"macos-proc-v1:%llu:%llu", info.pbi_start_tvsec, info.pbi_start_tvusec];
}
static BOOL recordMatches(NSDictionary *row, NSDictionary *target, NSString *token) {
    return [row[(__bridge NSString *)kCGWindowNumber] unsignedIntValue] == [target[@"number"] unsignedIntValue]
        && [row[(__bridge NSString *)kCGWindowOwnerPID] intValue] == [target[@"pid"] intValue]
        && [row[(__bridge NSString *)kCGWindowLayer] intValue] == 0
        && [row[(__bridge NSString *)kCGWindowOwnerName] isKindOfClass:[NSString class]]
        && [row[(__bridge NSString *)kCGWindowOwnerName] caseInsensitiveCompare:target[@"app"]] == NSOrderedSame
        && [token isEqualToString:target[@"process_instance"]];
}
static NSDictionary *verifyIdentity(NSDictionary *target) {
    withinBudget();
    CGWindowID number = [target[@"number"] unsignedIntValue];
    const void *windowPointer = (const void *)(uintptr_t)number;
    CFArrayRef wanted = CFArrayCreate(NULL, &windowPointer, 1, NULL);
    CFArrayRef descriptions = CGWindowListCreateDescriptionFromArray(wanted);
    CFRelease(wanted);
    if (!descriptions) fail(@"IDENTITY_UNAVAILABLE", @"Exact CoreGraphics window lookup failed");
    NSArray *rows = CFBridgingRelease(descriptions);
    if (rows.count != 1) fail(@"TARGET_CHANGED", @"The enrolled native window is absent or ambiguous");
    NSDictionary *row = rows.firstObject;
    if (![row isKindOfClass:[NSDictionary class]] || !recordMatches(row, target, processToken([target[@"pid"] intValue])))
        fail(@"TARGET_CHANGED", @"Window owner or process generation differs from enrollment");
    withinBudget();
    return row;
}
static void axPrepare(AXUIElementRef element) {
    withinBudget();
    AXError error = AXUIElementSetMessagingTimeout(element, (float)MIN(0.35, deadline-now()));
    if (error != kAXErrorSuccess) fail(@"AX_UNAVAILABLE", @"Could not bound native accessibility messaging");
}
static id axRead(AXUIElementRef element, CFStringRef attribute) {
    axPrepare(element);
    CFTypeRef result = NULL;
    AXError error = AXUIElementCopyAttributeValue(element, attribute, &result);
    withinBudget();
    if (error == kAXErrorSuccess) return CFBridgingRelease(result);
    if (result) CFRelease(result);
    if (error == kAXErrorAttributeUnsupported || error == kAXErrorNoValue) return nil;
    if (error == kAXErrorAPIDisabled) fail(@"PERM_DENIED", @"Accessibility access is unavailable to the recovery helper");
    if (error == kAXErrorCannotComplete || error == kAXErrorInvalidUIElement)
        fail(@"AX_UNAVAILABLE", @"The enrolled process has not exposed stable accessibility data");
    fail(@"AX_FAILURE", @"Unexpected accessibility response during exact-window recovery");
    return nil;
}
static CGWindowID axNumber(AXUIElementRef element, WindowBridge bridge) {
    axPrepare(element);
    CGWindowID number = 0;
    AXError error = bridge(element, &number);
    withinBudget();
    if (error == kAXErrorSuccess) return number;
    if (error == kAXErrorAttributeUnsupported || error == kAXErrorNoValue || error == kAXErrorIllegalArgument) return 0;
    if (error == kAXErrorAPIDisabled) fail(@"PERM_DENIED", @"Accessibility access is unavailable to the recovery helper");
    fail(@"AX_UNAVAILABLE", @"Could not verify the accessibility window's native ID");
    return 0;
}
static void addElements(NSMutableArray *elements, id value) {
    NSArray *candidates = [value isKindOfClass:[NSArray class]] ? value : (value ? @[value] : @[]);
    for (id candidate in candidates) {
        if (CFGetTypeID((__bridge CFTypeRef)candidate) != AXUIElementGetTypeID()) continue;
        BOOL duplicate = NO;
        for (id existing in elements) if (CFEqual((__bridge CFTypeRef)candidate, (__bridge CFTypeRef)existing)) duplicate = YES;
        if (!duplicate) [elements addObject:candidate];
    }
}
static id exactAXWindow(AXUIElementRef app, CGWindowID number, WindowBridge bridge) {
    NSMutableArray *elements = [NSMutableArray array];
    for (NSString *attribute in @[@"AXWindows", @"AXMainWindow", @"AXFocusedWindow", @"AXChildren"])
        addElements(elements, axRead(app, (__bridge CFStringRef)attribute));
    id match = nil;
    for (id value in elements) {
        AXUIElementRef element = (__bridge AXUIElementRef)value;
        if (![axRead(element, kAXRoleAttribute) isEqual:@"AXWindow"]) continue;
        if (axNumber(element, bridge) != number) continue;
        if (match) fail(@"AMBIGUOUS_TARGET", @"More than one accessibility window has the enrolled native ID");
        match = value;
    }
    return match;
}
typedef id (^PageRead)(id node, CFStringRef attribute);
static id uniqueOuterPageElement(id window, PageRead read) {
    // Stop at each first webarea. Descendant iframe documents cannot supply
    // the browser's origin, and their content need not be traversed at all.
    NSMutableArray *pending=[NSMutableArray arrayWithObject:@{@"node":window,@"depth":@0}];
    NSMutableArray *seen=[NSMutableArray array], *pages=[NSMutableArray array];
    while (pending.count) {
        withinBudget();
        NSDictionary *item=pending.lastObject; [pending removeLastObject];
        id node=item[@"node"]; NSUInteger depth=[item[@"depth"] unsignedIntegerValue];
        if ([seen containsObject:node]) continue;
        if (seen.count>=256 || depth>24)
            fail(@"PAGE_COVERAGE_INCOMPLETE", @"The browser chrome exceeded the bounded page search");
        [seen addObject:node];
        id role=read(node,kAXRoleAttribute);
        if (![role isKindOfClass:[NSString class]])
            fail(@"PAGE_COVERAGE_INCOMPLETE", @"A browser chrome node has no verified accessibility role");
        if ([role isEqual:@"AXWebArea"]) {
            [pages addObject:node];
            if (pages.count>1) fail(@"PAGE_NOT_UNIQUE", @"More than one outer webarea is exposed in the enrolled window");
            continue;
        }
        id children=read(node,kAXChildrenAttribute);
        if (children && ![children isKindOfClass:[NSArray class]])
            fail(@"PAGE_COVERAGE_INCOMPLETE", @"Browser chrome children could not be inspected completely");
        for (id child in children ?: @[]) {
            if (pending.count>=256)
                fail(@"PAGE_COVERAGE_INCOMPLETE", @"The browser chrome exceeded the bounded page search");
            [pending addObject:@{@"node":child,@"depth":@(depth+1)}];
        }
    }
    if (pages.count!=1) fail(@"PAGE_NOT_UNIQUE", @"No unique outer webarea is exposed in the enrolled window");
    return pages.firstObject;
}
static NSDictionary *uniqueOuterPage(id window, PageRead read) {
    __block NSUInteger count=0;
    id page=uniqueOuterPageElement(window,^id(id node,CFStringRef attribute) {
        if (CFEqual(attribute,kAXRoleAttribute)) count++;
        return read(node,attribute);
    });
    id url=read(page,CFSTR("AXURL"));
    // AXURL is supplied by the browser accessibility implementation. Neither
    // an element's title nor text resembling a URL is accepted here.
    if (![url isKindOfClass:[NSURL class]] || ![url absoluteString].length)
        fail(@"PAGE_URL_UNAVAILABLE", @"The outer webarea did not expose a native URL");
    withinBudget();
    return @{@"page_url":[url absoluteString],@"page_role":@"AXWebArea",
             @"outer_webarea_count":@1,@"page_complete":@YES,@"chrome_nodes_inspected":@(count)};
}
static NSDictionary *inspectPage(NSDictionary *target) {
    diagnostic[@"phase"]=@"inspecting_page";
    if (!AXIsProcessTrusted()) fail(@"PERM_DENIED", @"Accessibility access is unavailable to the page reader");
    WindowBridge bridge=(WindowBridge)dlsym(RTLD_DEFAULT,"_AXUIElementGetWindow");
    if (!bridge) fail(@"ACTION_NOT_SUPPORTED", @"Exact AX-to-native window matching is unavailable");
    verifyIdentity(target);
    AXUIElementRef app=AXUIElementCreateApplication([target[@"pid"] intValue]);
    @try {
        id exact=exactAXWindow(app,[target[@"number"] unsignedIntValue],bridge);
        if (!exact) fail(@"AX_UNAVAILABLE", @"The enrolled window is not exposed through accessibility");
        NSDictionary *page=uniqueOuterPage(exact,^id(id node,CFStringRef attribute) {
            if (CFGetTypeID((__bridge CFTypeRef)node)!=AXUIElementGetTypeID())
                fail(@"PAGE_COVERAGE_INCOMPLETE", @"A browser chrome child is not an accessibility element");
            return axRead((__bridge AXUIElementRef)node,attribute);
        });
        if (axNumber((__bridge AXUIElementRef)exact,bridge)!=[target[@"number"] unsignedIntValue])
            fail(@"TARGET_CHANGED", @"The inspected accessibility window changed");
        verifyIdentity(target);
        return page;
    } @finally { CFRelease(app); }
}
static BOOL matchesPattern(NSString *value, NSString *pattern) {
    if (![value isKindOfClass:[NSString class]]) return NO;
    return [[NSRegularExpression regularExpressionWithPattern:pattern options:0 error:nil]
            firstMatchInString:value options:0 range:NSMakeRange(0,value.length)] != nil;
}
static NSDictionary *validatedFieldRequest(id value) {
    if (![value isKindOfClass:[NSDictionary class]]) fail(@"INVALID_REQUEST", @"Field request must be a JSON object");
    NSDictionary *request=value;
    NSSet *allowed=[NSSet setWithArray:@[@"field",@"value",@"expected_value",@"expected_url_prefix",@"expected_account_label",@"expected_recipient"]];
    for (id key in request) if (![allowed containsObject:key]) fail(@"INVALID_REQUEST", @"Unexpected field request property");
    for (NSString *key in @[@"field",@"value",@"expected_value",@"expected_url_prefix",@"expected_account_label"])
        if (![request[key] isKindOfClass:[NSString class]]) fail(@"INVALID_REQUEST", @"Required field request property is not a string");
    NSString *field=request[@"field"];
    if (![field isEqual:@"Type a message"] && ![field isEqual:@"Type a name or phone number"])
        fail(@"INVALID_REQUEST", @"Only Google Voice message fields are supported");
    for (NSString *key in @[@"value",@"expected_value"])
        if ([request[key] length]>1600 || [request[key] rangeOfString:[NSString stringWithFormat:@"%C",(unichar)0]].location!=NSNotFound)
            fail(@"INVALID_REQUEST", @"Field value exceeds the supported limit");
    if (![request[@"expected_account_label"] length] || [request[@"expected_account_label"] length]>1000
        || !matchesPattern(request[@"expected_url_prefix"],@"^https://voice\\.google\\.com/u/[0-9]+/$"))
        fail(@"INVALID_REQUEST", @"A verified Google Voice account and account URL are required");
    if ([field isEqual:@"Type a message"]) {
        if (!matchesPattern(request[@"expected_recipient"],@"^\\+[1-9][0-9]{7,14}$"))
            fail(@"INVALID_REQUEST", @"An exact message recipient is required");
    } else if (request[@"expected_recipient"] || !matchesPattern(request[@"value"],@"^\\+[1-9][0-9]{7,14}$")) {
        fail(@"INVALID_REQUEST", @"Recipient entry requires one exact international number");
    }
    return request;
}
static NSDictionary *readFieldRequest(void) {
    NSMutableData *data=[NSMutableData data];
    for (;;) {
        withinBudget();
        struct pollfd input={.fd=STDIN_FILENO,.events=POLLIN};
        int wait=(int)MAX(1,MIN(100,(deadline-now())*1000));
        int ready=poll(&input,1,wait);
        if (ready<0 && errno==EINTR) continue;
        if (ready<0 || (input.revents&(POLLERR|POLLNVAL))) fail(@"INVALID_REQUEST", @"Could not read the private field request");
        if (!ready) continue;
        char buffer[4096]; ssize_t count=read(STDIN_FILENO,buffer,sizeof(buffer));
        if (count<0 && errno==EINTR) continue;
        if (count<0) fail(@"INVALID_REQUEST", @"Could not read the private field request");
        if (!count) break;
        [data appendBytes:buffer length:(NSUInteger)count];
        if (data.length>65536) fail(@"INVALID_REQUEST", @"Private field request exceeds its supported limit");
    }
    id value=[NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return validatedFieldRequest(value);
}
static BOOL containsFieldNode(NSArray *nodes, id candidate) {
    for (id node in nodes) {
        if (node==candidate) return YES;
        if (CFGetTypeID((__bridge CFTypeRef)node)==AXUIElementGetTypeID()
            && CFGetTypeID((__bridge CFTypeRef)candidate)==AXUIElementGetTypeID()
            && CFEqual((__bridge CFTypeRef)node,(__bridge CFTypeRef)candidate)) return YES;
    }
    return NO;
}
static NSArray *completePageNodes(id root, PageRead read) {
    NSMutableArray *pending=[NSMutableArray arrayWithObject:@{@"node":root,@"depth":@0,@"ancestors":@[]}], *result=[NSMutableArray array];
    while (pending.count) {
        withinBudget();
        NSDictionary *item=pending.lastObject; [pending removeLastObject]; id node=item[@"node"];
        NSUInteger depth=[item[@"depth"] unsignedIntegerValue];
        NSArray *ancestors=item[@"ancestors"];
        if (containsFieldNode(ancestors,node)) { diagnostic[@"coverage_reason"]=@"cycle"; fail(@"FIELD_COVERAGE_INCOMPLETE", @"The message accessibility tree contains a cycle"); }
        // Chrome may expose a single AX element through multiple branches.
        // Count that element once; distinct controls remain distinct candidates.
        if (containsFieldNode(result,node)) continue;
        if (result.count>=2048 || depth>60) { diagnostic[@"coverage_reason"]=@"tree_bound"; fail(@"FIELD_COVERAGE_INCOMPLETE", @"The message accessibility tree exceeded its bound"); }
        id role=read(node,kAXRoleAttribute);
        if (![role isKindOfClass:[NSString class]]) { diagnostic[@"coverage_reason"]=@"missing_role"; fail(@"FIELD_COVERAGE_INCOMPLETE", @"A message node has no verified role"); }
        // A nested frame can never supply our account, recipient or composer.
        if (node!=root && [role isEqual:@"AXWebArea"]) continue;
        [result addObject:node];
        id children=read(node,kAXChildrenAttribute);
        if (children && ![children isKindOfClass:[NSArray class]]) { diagnostic[@"coverage_reason"]=@"invalid_children"; fail(@"FIELD_COVERAGE_INCOMPLETE", @"Message children are unavailable"); }
        if (pending.count+[children count]+result.count>4096) { diagnostic[@"coverage_reason"]=@"tree_bound"; fail(@"FIELD_COVERAGE_INCOMPLETE", @"The message accessibility tree exceeded its bound"); }
        for (id child in children ?: @[]) [pending addObject:@{@"node":child,@"depth":@(depth+1),@"ancestors":[ancestors arrayByAddingObject:node]}];
    }
    return result;
}
static NSString *fieldLabel(id node, PageRead read) {
    for (NSString *attribute in @[@"AXTitle",@"AXDescription",@"AXPlaceholderValue"]){
        id value=read(node,(__bridge CFStringRef)attribute);
        if ([value isKindOfClass:[NSString class]] && [value length]) return value;
    }
    return nil;
}
static NSArray *matchingNodes(NSArray *nodes, NSString *role, NSString *label, PageRead read) {
    NSMutableArray *matches=[NSMutableArray array];
    for (id node in nodes) if ([read(node,kAXRoleAttribute) isEqual:role]
        && (!label || [fieldLabel(node,read) isEqual:label])) [matches addObject:node];
    return matches;
}
static NSString *normalizedPhone(NSString *value) {
    if (!matchesPattern(value,@"^[+()0-9 .-]+$")) return nil;
    NSString *digits=[[value componentsSeparatedByCharactersInSet:[[NSCharacterSet characterSetWithCharactersInString:@"0123456789"] invertedSet]] componentsJoinedByString:@""];
    if (digits.length==10) return [@"+1" stringByAppendingString:digits];
    if (digits.length==11 && [digits hasPrefix:@"1"]) return [@"+" stringByAppendingString:digits];
    if ([value hasPrefix:@"+"] && digits.length>=8 && digits.length<=15) return [@"+" stringByAppendingString:digits];
    return nil;
}
static void verifyMessageURL(id page, NSDictionary *request, PageRead read) {
    id url=read(page,CFSTR("AXURL"));
    if (![url isKindOfClass:[NSURL class]]) fail(@"FIELD_ORIGIN_UNVERIFIED", @"Message page URL is unavailable");
    NSURLComponents *actual=[NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
    NSString *path=[[request[@"expected_url_prefix"] substringFromIndex:[@"https://voice.google.com" length]] stringByAppendingString:@"messages"];
    if (![actual.scheme isEqual:@"https"] || ![actual.host isEqual:@"voice.google.com"]
        || actual.port || actual.user || actual.password || ![actual.path isEqual:path]
        || ![fieldLabel(page,read) isEqual:@"Voice - Messages"])
        fail(@"FIELD_ORIGIN_UNVERIFIED", @"The exact Google Voice Messages page is not present");
}
static id selectMessageField(id window, NSDictionary *request, PageRead read) {
    id page=uniqueOuterPageElement(window,read); verifyMessageURL(page,request,read);
    NSArray *nodes=completePageNodes(page,read);
    if (matchingNodes(nodes,@"AXButton",request[@"expected_account_label"],read).count!=1)
        fail(@"FIELD_ACCOUNT_UNVERIFIED", @"The expected message account is absent or ambiguous");
    NSArray *details=matchingNodes(nodes,@"AXGroup",@"Details",read);
    if (details.count!=1) fail(@"FIELD_NOT_UNIQUE", @"Message details are absent or ambiguous");
    NSArray *detailNodes=completePageNodes(details.firstObject,read);
    NSArray *selectors=matchingNodes(detailNodes,@"AXGroup",@"Select recipients",read);
    if (selectors.count!=1) { diagnostic[@"recipient_reason"]=@"selector_ambiguous"; fail(@"FIELD_RECIPIENT_UNVERIFIED", @"The recipient selector is absent or ambiguous"); }
    NSArray *rows=matchingNodes(completePageNodes(selectors.firstObject,read),@"AXRow",nil,read);
    if ([request[@"field"] isEqual:@"Type a message"]) {
        if (rows.count!=1) { diagnostic[@"recipient_reason"]=@"row_count"; fail(@"FIELD_RECIPIENT_UNVERIFIED", @"Exactly one committed recipient is required"); }
        NSString *prefix=[[fieldLabel(rows.firstObject,read) componentsSeparatedByString:@" Remove "] firstObject];
        if (![normalizedPhone(prefix) isEqual:request[@"expected_recipient"]])
            { diagnostic[@"recipient_reason"]=@"row_label"; fail(@"FIELD_RECIPIENT_UNVERIFIED", @"The committed recipient differs from the request"); }
        id cells=read(rows.firstObject,kAXChildrenAttribute); NSUInteger verifiedCells=0;
        if (![cells isKindOfClass:[NSArray class]]) { diagnostic[@"coverage_reason"]=@"recipient_cells"; fail(@"FIELD_COVERAGE_INCOMPLETE", @"Recipient cells are unavailable"); }
        for (id cell in cells ?: @[]) if ([read(cell,kAXRoleAttribute) isEqual:@"AXCell"]) {
            NSMutableArray *labels=[NSMutableArray array];
            NSString *label=fieldLabel(cell,read);
            if (label.length) [labels addObject:label];
            // Chrome's chip cell can be unnamed in AX; the number is exposed
            // as static text below a group. The neighboring remove-button
            // cell contains no static text and cannot verify a recipient.
            for (id child in completePageNodes(cell,read)) if ([read(child,kAXRoleAttribute) isEqual:@"AXStaticText"]) {
                id value=read(child,kAXValueAttribute);
                if (![value isKindOfClass:[NSString class]] || ![value length]) value=fieldLabel(child,read);
                if ([value isKindOfClass:[NSString class]] && [value length]) [labels addObject:value];
            }
            for (NSString *value in labels) {
                if (![normalizedPhone(value) isEqual:request[@"expected_recipient"]])
                    { diagnostic[@"recipient_reason"]=@"cell_mismatch"; fail(@"FIELD_RECIPIENT_UNVERIFIED", @"Recipient cells disagree with the request"); }
            }
            if (labels.count) verifiedCells++;
        }
        if (!verifiedCells) { diagnostic[@"recipient_reason"]=@"no_cells"; fail(@"FIELD_RECIPIENT_UNVERIFIED", @"No recipient cell verifies the request"); }
    } else if (rows.count) { diagnostic[@"recipient_reason"]=@"entry_has_rows"; fail(@"FIELD_RECIPIENT_UNVERIFIED", @"Recipient entry already contains a committed recipient"); }
    NSMutableArray *fields=[matchingNodes(detailNodes,@"AXTextField",request[@"field"],read) mutableCopy];
    if ([request[@"field"] isEqual:@"Type a message"])
        [fields addObjectsFromArray:matchingNodes(detailNodes,@"AXTextArea",request[@"field"],read)];
    if (fields.count!=1) fail(@"FIELD_NOT_UNIQUE", @"The requested message field is absent or ambiguous");
    return fields.firstObject;
}
static void verifyAXRoot(AXUIElementRef window, NSDictionary *target, WindowBridge bridge) {
    axPrepare(window); pid_t owner=0; AXError error=AXUIElementGetPid(window,&owner); withinBudget();
    if (error!=kAXErrorSuccess || owner!=[target[@"pid"] intValue]
        || axNumber(window,bridge)!=[target[@"number"] unsignedIntValue])
        fail(@"TARGET_CHANGED", @"The accessibility root owner or native window differs from enrollment");
    verifyIdentity(target);
}
static void verifyExpectedFieldValue(id field, NSString *expected, PageRead read) {
    id current=read(field,kAXValueAttribute);
    if (![current isKindOfClass:[NSString class]] || ![current isEqual:expected])
        fail(@"FIELD_VALUE_CHANGED", @"The existing field value differs from the expected value");
}
static NSDictionary *setMessageField(NSDictionary *target, NSDictionary *request) {
    if (![target[@"app"] isEqual:@"Google Chrome"]) fail(@"INVALID_REQUEST", @"Only the enrolled Google Chrome window supports message entry");
    if (!AXIsProcessTrusted()) fail(@"PERM_DENIED", @"Accessibility access is unavailable to the message field helper");
    WindowBridge bridge=(WindowBridge)dlsym(RTLD_DEFAULT,"_AXUIElementGetWindow");
    if (!bridge) fail(@"ACTION_NOT_SUPPORTED", @"Exact AX-to-native window matching is unavailable");
    verifyIdentity(target);
    AXUIElementRef app=AXUIElementCreateApplication([target[@"pid"] intValue]);
    @try {
        id exact=exactAXWindow(app,[target[@"number"] unsignedIntValue],bridge);
        if (!exact) fail(@"AX_UNAVAILABLE", @"The enrolled message window is unavailable");
        verifyAXRoot((__bridge AXUIElementRef)exact,target,bridge);
        PageRead reader=^id(id node,CFStringRef attribute) {
            if (CFGetTypeID((__bridge CFTypeRef)node)!=AXUIElementGetTypeID()) { diagnostic[@"coverage_reason"]=@"invalid_element"; fail(@"FIELD_COVERAGE_INCOMPLETE", @"A message child is not an accessibility element"); }
            return axRead((__bridge AXUIElementRef)node,attribute);
        };
        diagnostic[@"phase"]=@"verifying_message_field";
        id field=selectMessageField(exact,request,reader);
        verifyExpectedFieldValue(field,request[@"expected_value"],reader);
        verifyAXRoot((__bridge AXUIElementRef)exact,target,bridge);
        AXUIElementRef element=(__bridge AXUIElementRef)field;
        axPrepare(element); Boolean settable=false;
        AXError supported=AXUIElementIsAttributeSettable(element,kAXValueAttribute,&settable); withinBudget();
        if (supported==kAXErrorAPIDisabled) fail(@"PERM_DENIED", @"Accessibility denied message field access");
        if (supported!=kAXErrorSuccess || !settable) fail(@"FIELD_NOT_SETTABLE", @"The message value is not directly settable");
        verifyExpectedFieldValue(field,request[@"expected_value"],reader);
        // One native set only: a timeout may have delivered it. Never retry.
        axPrepare(element); fieldSetDispatched=YES; diagnostic[@"phase"]=@"setting_message_field";
        AXError changed=AXUIElementSetAttributeValue(element,kAXValueAttribute,(__bridge CFTypeRef)request[@"value"]);
        if (changed==kAXErrorAPIDisabled) fail(@"PERM_DENIED", @"Accessibility denied the message field change");
        if (changed!=kAXErrorSuccess && changed!=kAXErrorCannotComplete) fail(@"FIELD_SET_UNVERIFIED", @"The message field change was not confirmed");
        withinBudget(); diagnostic[@"phase"]=@"verifying_message_value";
        id after=selectMessageField(exact,request,reader);
        if (!CFEqual((__bridge CFTypeRef)field,(__bridge CFTypeRef)after)
            || ![reader(after,kAXValueAttribute) isEqual:request[@"value"]])
            fail(@"FIELD_SET_UNVERIFIED", @"The exact message field did not confirm the requested value");
        verifyAXRoot((__bridge AXUIElementRef)exact,target,bridge);
        return @{@"field_verified":@YES,@"value_set":@YES,@"value_verified":@YES,
                 @"value_length":@([request[@"value"] length]),@"previous_length":@([request[@"expected_value"] length])};
    } @finally { CFRelease(app); }
}
static int acquireLease(void) {
    NSString *directory = [NSString stringWithFormat:@"/tmp/agent-desktop-%u", geteuid()];
    if (mkdir(directory.fileSystemRepresentation, 0700) && errno != EEXIST)
        fail(@"LOCK_UNAVAILABLE", @"Could not create the shared desktop interaction directory");
    struct stat metadata;
    if (lstat(directory.fileSystemRepresentation, &metadata) || !S_ISDIR(metadata.st_mode)
        || metadata.st_uid != geteuid() || (metadata.st_mode & 0077))
        fail(@"LOCK_UNAVAILABLE", @"Shared desktop interaction directory has unsafe ownership or mode");
    NSString *path = [directory stringByAppendingPathComponent:@"interaction.lock"];
    int fd = open(path.fileSystemRepresentation, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) fail(@"LOCK_UNAVAILABLE", @"Could not open the shared desktop interaction lock");
    if (fstat(fd, &metadata) || !S_ISREG(metadata.st_mode) || metadata.st_uid != geteuid() || (metadata.st_mode & 0077)) {
        close(fd); fail(@"LOCK_UNAVAILABLE", @"Shared desktop interaction lock has unsafe ownership or mode");
    }
    while (flock(fd, LOCK_EX | LOCK_NB)) {
        if (errno != EWOULDBLOCK && errno != EAGAIN) { close(fd); fail(@"LOCK_UNAVAILABLE", @"Desktop interaction lock failed"); }
        if (now() >= deadline) { close(fd); fail(@"TIMEOUT", @"Desktop interaction lock is busy"); }
        usleep(10000);
    }
    return fd;
}
static NSDictionary *parseArguments(NSArray<NSString *> *args) {
    NSMutableDictionary *values = [NSMutableDictionary dictionary];
    NSSet *allowed = [NSSet setWithArray:@[@"--app", @"--window-id", @"--pid", @"--process-instance", @"--budget-ms"]];
    for (NSUInteger i = 1; i < args.count; i += 2) {
        if (i+1 >= args.count || ![allowed containsObject:args[i]] || values[args[i]])
            fail(@"INVALID_ARGUMENT", @"Expected unique --app --window-id --pid --process-instance and optional --budget-ms");
        values[args[i]] = args[i+1];
    }
    NSString *app = values[@"--app"], *wid = values[@"--window-id"], *pidText = values[@"--pid"], *token = values[@"--process-instance"];
    NSRegularExpression *windowPattern = [NSRegularExpression regularExpressionWithPattern:@"^w-[1-9][0-9]*$" options:0 error:nil];
    NSCharacterSet *nondigits = [[NSCharacterSet decimalDigitCharacterSet] invertedSet];
    NSString *budgetText = values[@"--budget-ms"] ?: @"6000";
    if (!app.length || [app rangeOfCharacterFromSet:[NSCharacterSet controlCharacterSet]].location != NSNotFound
        || !wid || ![windowPattern firstMatchInString:wid options:0 range:NSMakeRange(0, wid.length)]
        || !pidText.length || [pidText rangeOfCharacterFromSet:nondigits].location != NSNotFound
        || !token.length || token.length > 512 || [token rangeOfCharacterFromSet:[NSCharacterSet controlCharacterSet]].location != NSNotFound
        || [budgetText rangeOfCharacterFromSet:nondigits].location != NSNotFound)
        fail(@"INVALID_ARGUMENT", @"A valid enrolled app, native window ID, PID and process-generation token are required");
    long long pid = pidText.longLongValue, number = [[wid substringFromIndex:2] longLongValue], budget = budgetText.longLongValue;
    if (pid < 1 || pid > INT32_MAX || number < 1 || number > UINT32_MAX || budget < 1 || budget > 15000)
        fail(@"INVALID_ARGUMENT", @"PID, window ID or recovery deadline is outside its supported range");
    return @{@"app":app, @"window_id":wid, @"number":@(number), @"pid":@(pid), @"process_instance":token, @"budget_ms":@(budget)};
}
static void recover(NSDictionary *target) {
    diagnostic[@"phase"]=@"preflight";
    if (!AXIsProcessTrusted()) fail(@"PERM_DENIED", @"Accessibility access is unavailable to the recovery helper");
    WindowBridge bridge = (WindowBridge)dlsym(RTLD_DEFAULT, "_AXUIElementGetWindow");
    if (!bridge) fail(@"ACTION_NOT_SUPPORTED", @"Exact AX-to-native window matching is unavailable");
    verifyIdentity(target);
    pid_t pid = [target[@"pid"] intValue];
    NSRunningApplication *running = [NSRunningApplication runningApplicationWithProcessIdentifier:pid];
    if (!running || running.terminated) fail(@"TARGET_CHANGED", @"The enrolled application process is no longer running");
    verifyIdentity(target);
    activationDispatched = YES;
    diagnostic[@"phase"]=@"activating";
    // Do not use ActivateAllWindows and do not launch an app by name.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    diagnostic[@"activation_accepted"]=@([running activateWithOptions:NSApplicationActivateIgnoringOtherApps]);
#pragma clang diagnostic pop
    AXUIElementRef app = AXUIElementCreateApplication(pid);
    @try {
        id exact = nil;
        diagnostic[@"phase"]=@"resolving_ax_window";
        while (!exact) {
            diagnostic[@"resolution_attempts"]=@([diagnostic[@"resolution_attempts"] intValue]+1);
            verifyIdentity(target);
            @try { exact = exactAXWindow(app, [target[@"number"] unsignedIntValue], bridge); }
            @catch (NSException *error) {
                diagnostic[@"last_ax_error"]=error.name;
                if (![error.name isEqual:@"AX_UNAVAILABLE"]) @throw;
            }
            if (!exact) pauseBriefly();
        }
        verifyIdentity(target);
        AXUIElementRef window = (__bridge AXUIElementRef)exact;
        if (axNumber(window, bridge) != [target[@"number"] unsignedIntValue])
            fail(@"TARGET_CHANGED", @"The resolved accessibility window changed before focus");
        axPrepare(window);
        raiseDispatched = YES;
        diagnostic[@"phase"]=@"raising";
        AXError raised = AXUIElementPerformAction(window, kAXRaiseAction);
        if (raised == kAXErrorAPIDisabled) fail(@"PERM_DENIED", @"Accessibility access was denied during focus");
        // A timeout may still have delivered the raise. Never repeat it; observe.
        if (raised != kAXErrorSuccess && raised != kAXErrorCannotComplete)
            fail(@"ACTION_NOT_SUPPORTED", @"The exact native window could not be raised");
        int confirmations = 0;
        diagnostic[@"phase"]=@"verifying_focus";
        while (confirmations < 2) {
            NSDictionary *row = verifyIdentity(target);
            BOOL exactFocused = NO;
            @try {
                id focused = axRead(app, kAXFocusedWindowAttribute);
                BOOL hasFocused = focused && CFGetTypeID((__bridge CFTypeRef)focused) == AXUIElementGetTypeID();
                CGWindowID focusedID = hasFocused ? axNumber((__bridge AXUIElementRef)focused, bridge) : 0;
                diagnostic[@"focused_window_id"]=@(focusedID);
                exactFocused = hasFocused && focusedID == [target[@"number"] unsignedIntValue];
            } @catch (NSException *error) {
                diagnostic[@"last_ax_error"]=error.name;
                if (![error.name isEqual:@"AX_UNAVAILABLE"]) @throw;
            }
            pid_t frontmost = [NSWorkspace sharedWorkspace].frontmostApplication.processIdentifier;
            BOOL isFront = frontmost == pid;
            BOOL onScreen = [row[(__bridge NSString *)kCGWindowIsOnscreen] boolValue];
            confirmations = exactFocused && isFront && onScreen ? confirmations+1 : 0;
            diagnostic[@"exact_focused"]=@(exactFocused);
            diagnostic[@"frontmost_pid"]=@(frontmost);
            diagnostic[@"onscreen"]=@(onScreen);
            diagnostic[@"confirmations"]=@(confirmations);
            if (confirmations < 2) pauseBriefly();
        }
    } @finally { CFRelease(app); }
}
static void selfTest(void) {
    NSDictionary *target = parseArguments(@[@"helper", @"--app", @"Google Chrome", @"--window-id", @"w-121", @"--pid", @"669", @"--process-instance", @"fixture"]);
    NSDictionary *row = @{(__bridge NSString *)kCGWindowNumber:@121, (__bridge NSString *)kCGWindowOwnerPID:@669,
                          (__bridge NSString *)kCGWindowOwnerName:@"Google Chrome", (__bridge NSString *)kCGWindowLayer:@0};
    if (!recordMatches(row,target,@"fixture")) fail(@"TEST_FAILED", @"Valid native identity was rejected");
    for (NSDictionary *change in @[@{(__bridge NSString *)kCGWindowNumber:@122}, @{(__bridge NSString *)kCGWindowOwnerPID:@670},
                                  @{(__bridge NSString *)kCGWindowOwnerName:@"Other"}, @{(__bridge NSString *)kCGWindowLayer:@1}]) {
        NSMutableDictionary *wrong = row.mutableCopy; [wrong addEntriesFromDictionary:change];
        if (recordMatches(wrong,target,@"fixture")) fail(@"TEST_FAILED", @"Wrong native identity was accepted");
    }
    if (recordMatches(row,target,@"recycled")) fail(@"TEST_FAILED", @"Recycled process was accepted");
    for (NSArray *args in @[@[@"helper"], @[@"helper",@"--pid",@"669"], @[@"helper",@"--unexpected",@"x"]]) {
        BOOL rejected = NO;
        @try { parseArguments(args); } @catch (NSException *error) { rejected = [error.name isEqual:@"INVALID_ARGUMENT"]; }
        if (!rejected) fail(@"TEST_FAILED", @"Incomplete target was accepted");
    }
    deadline=now()+2;
    PageRead fixture=^id(id node,CFStringRef attribute) { return node[(__bridge NSString *)attribute]; };
    NSDictionary *page=@{@"AXRole":@"AXWebArea",@"AXURL":[NSURL URLWithString:@"https://voice.google.com/u/0/calls"],
                         @"AXChildren":@[@{@"AXRole":@"AXWebArea",@"AXURL":[NSURL URLWithString:@"https://unrelated.invalid/"]}]};
    NSDictionary *root=@{@"AXRole":@"AXWindow",@"AXChildren":@[page]};
    NSDictionary *result=uniqueOuterPage(root,fixture);
    if (![result[@"page_url"] isEqual:@"https://voice.google.com/u/0/calls"] || [result[@"chrome_nodes_inspected"] intValue]!=2)
        fail(@"TEST_FAILED", @"Iframe origin was read instead of the outer page");
    for (NSDictionary *bad in @[
        @{@"root":@{@"AXRole":@"AXWindow",@"AXChildren":@[page,@{@"AXRole":@"AXWebArea",@"AXURL":[NSURL URLWithString:@"https://second.invalid/"]}]},@"code":@"PAGE_NOT_UNIQUE"},
        @{@"root":@{@"AXRole":@"AXWindow"},@"code":@"PAGE_NOT_UNIQUE"},
        @{@"root":@{@"AXRole":@"AXWebArea",@"AXURL":@"https://voice.google.com/u/0/calls"},@"code":@"PAGE_URL_UNAVAILABLE"},
        @{@"root":@{@"AXRole":@"AXWindow",@"AXChildren":@"incomplete"},@"code":@"PAGE_COVERAGE_INCOMPLETE"},
        @{@"root":@{@"AXChildren":@[page]},@"code":@"PAGE_COVERAGE_INCOMPLETE"}]) {
        BOOL rejected=NO;
        @try { uniqueOuterPage(bad[@"root"],fixture); }
        @catch (NSException *error) { rejected=[error.name isEqual:bad[@"code"]]; }
        if (!rejected) fail(@"TEST_FAILED", @"Ambiguous or incomplete page proof was accepted");
    }
    id deep=page;
    for (int i=0;i<26;i++) deep=@{@"AXRole":@"AXGroup",@"AXChildren":@[deep]};
    NSMutableArray *wide=[NSMutableArray array];
    for (int i=0;i<257;i++) [wide addObject:@{@"AXRole":@"AXButton",@"fixture_id":@(i)}];
    for (id excessive in @[deep,@{@"AXRole":@"AXWindow",@"AXChildren":wide}]) {
        BOOL rejected=NO;
        @try { uniqueOuterPage(excessive,fixture); }
        @catch (NSException *error) { rejected=[error.name isEqual:@"PAGE_COVERAGE_INCOMPLETE"]; }
        if (!rejected) fail(@"TEST_FAILED", @"An unbounded browser chrome tree was accepted");
    }
    deadline=now()-1;
    BOOL timedOut=NO;
    @try { uniqueOuterPage(root,fixture); }
    @catch (NSException *error) { timedOut=[error.name isEqual:@"TIMEOUT"]; }
    if (!timedOut) fail(@"TEST_FAILED", @"Page inspection ignored its shared deadline");
    emit(@{@"ok":@YES, @"self_test":@"passed", @"native_actions":@0,@"page_fixture_tests":@9});
}
#include "native_field_tests.h"
int main(void) {
    @autoreleasepool {
        int lease = -1; double began = now(); NSDictionary *target = nil;
        diagnostic=[NSMutableDictionary dictionaryWithObject:@"arguments" forKey:@"phase"];
        @try {
            NSArray *args = [NSProcessInfo processInfo].arguments;
            if (args.count == 2 && [args[1] isEqual:@"--self-test"]) { selfTest(); return 0; }
            if (args.count == 2 && [args[1] isEqual:@"--field-self-test"]) { fieldSelfTest(); return 0; }
            BOOL inspectOnly = args.count > 1 && [args[1] isEqual:@"--inspect-identity"];
            BOOL pageOnly = args.count > 1 && [args[1] isEqual:@"--inspect-page"];
            BOOL fieldOnly = args.count > 1 && [args[1] isEqual:@"--set-field-stdin"];
            fieldRequestMode=fieldOnly;
            if (inspectOnly || pageOnly || fieldOnly) {
                NSMutableArray *parsedArgs=args.mutableCopy;
                [parsedArgs removeObjectAtIndex:1];
                args=parsedArgs;
            }
            target = parseArguments(args); deadline = began + [target[@"budget_ms"] doubleValue]/1000.0;
            if (fieldOnly) {
                NSDictionary *request=readFieldRequest(); diagnostic[@"field_kind"]=[request[@"field"] isEqual:@"Type a message"] ? @"body" : @"recipient";
                diagnostic[@"phase"]=@"acquiring_lease"; lease=acquireLease();
                NSMutableDictionary *receipt=[setMessageField(target,request) mutableCopy];
                [receipt addEntriesFromDictionary:@{@"ok":@YES,@"identity_verified":@YES,@"target":target,
                    @"set_dispatched":@(fieldSetDispatched),@"activation_dispatched":@NO,@"raise_dispatched":@NO,
                    @"elapsed_ms":@((now()-began)*1000),@"diagnostic":diagnostic}];
                emit(receipt); close(lease); return 0;
            }
            if (inspectOnly) {
                // Pure metadata read. In particular, do not acquire an input
                // lease, check/request AX permission, enumerate AX, or activate
                // any application. The caller still performs its normal AX
                // snapshot under the same independently enforced permissions.
                diagnostic[@"phase"]=@"inspecting_identity";
                verifyIdentity(target);
                emit(@{@"ok":@YES,@"identity_verified":@YES,@"target":target,
                       @"activation_dispatched":@NO,@"raise_dispatched":@NO,
                       @"elapsed_ms":@((now()-began)*1000),@"diagnostic":diagnostic});
                return 0;
            }
            if (pageOnly) {
                NSDictionary *page=inspectPage(target);
                NSMutableDictionary *receipt=[page mutableCopy];
                [receipt addEntriesFromDictionary:@{@"ok":@YES,@"identity_verified":@YES,@"target":target,
                    @"activation_dispatched":@NO,@"raise_dispatched":@NO,
                    @"elapsed_ms":@((now()-began)*1000),@"diagnostic":diagnostic}];
                emit(receipt); return 0;
            }
            diagnostic[@"phase"]=@"acquiring_lease";
            lease = acquireLease(); recover(target);
            emit(@{@"ok":@YES, @"status":@"verified", @"identity_verified":@YES, @"exact_window_focused":@YES,
                   @"target":target, @"activation_dispatched":@(activationDispatched), @"raise_dispatched":@(raiseDispatched),
                   @"elapsed_ms":@((now()-began)*1000), @"diagnostic":diagnostic});
            close(lease); return 0;
        } @catch (NSException *error) {
            if (lease >= 0) close(lease);
            emit(@{@"ok":@NO, @"status":@"unavailable", @"identity_verified":@NO, @"exact_window_focused":@NO,
                   @"target":target ?: @{}, @"activation_dispatched":@(activationDispatched), @"raise_dispatched":@(raiseDispatched),
                   @"set_dispatched":@(fieldSetDispatched),@"field_verified":@NO,@"value_set":@NO,@"value_verified":@NO,
                   @"error":@{@"code":error.name ?: @"UNEXPECTED_ERROR", @"message":fieldRequestMode ? @"Private message field operation could not be verified" : (error.reason ?: @"Exact-window recovery failed")},
                   @"elapsed_ms":@((now()-began)*1000), @"diagnostic":diagnostic});
            return 1;
        }
    }
}
