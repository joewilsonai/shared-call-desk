// Offline fixtures only. Included after the private field implementation.
static NSMutableDictionary *fieldFixture(void) {
    NSMutableDictionary *body=[@{@"AXRole":@"AXTextArea",@"AXTitle":@"Type a message",@"AXValue":@"",@"fixture_id":@"body"} mutableCopy];
    NSMutableDictionary *row=[@{@"AXRole":@"AXRow",@"AXTitle":@"(202) 555-0186 Remove (202) 555-0186",
        @"AXChildren":@[@{@"AXRole":@"AXCell",@"AXTitle":@"(202) 555-0186",@"fixture_id":@1},
                         @{@"AXRole":@"AXCell",@"AXTitle":@"(202) 555-0186",@"fixture_id":@2}]} mutableCopy];
    NSMutableDictionary *selector=[@{@"AXRole":@"AXGroup",@"AXTitle":@"Select recipients",@"AXChildren":@[row]} mutableCopy];
    NSMutableDictionary *details=[@{@"AXRole":@"AXGroup",@"AXTitle":@"Details",@"AXChildren":@[selector,body]} mutableCopy];
    NSMutableDictionary *account=[@{@"AXRole":@"AXButton",@"AXDescription":@"Google Account: Fixture"} mutableCopy];
    NSMutableDictionary *page=[@{@"AXRole":@"AXWebArea",@"AXTitle":@"Voice - Messages",
        @"AXURL":[NSURL URLWithString:@"https://voice.google.com/u/0/messages?itemId=draft"],@"AXChildren":@[account,details]} mutableCopy];
    return [@{@"AXRole":@"AXWindow",@"AXChildren":@[page]} mutableCopy];
}
static NSDictionary *fieldRequest(void) {
    return @{@"field":@"Type a message",@"value":@"Private \"text\"\n$() \\ unchanged",@"expected_value":@"",
             @"expected_url_prefix":@"https://voice.google.com/u/0/",@"expected_account_label":@"Google Account: Fixture",
             @"expected_recipient":@"+12025550186"};
}
static void fieldReject(NSString *code, void (^operation)(void)) {
    BOOL rejected=NO;
    @try { operation(); } @catch (NSException *error) { rejected=[error.name isEqual:code]; }
    if (!rejected) fail(@"TEST_FAILED", @"A private message field failure was not rejected correctly");
}
static void fieldSelfTest(void) {
    deadline=now()+10;
    PageRead read=^id(id node,CFStringRef key) { return node[(__bridge NSString *)key]; };
    NSDictionary *request=validatedFieldRequest(fieldRequest());
    NSMutableDictionary *window=fieldFixture();
    id selected=selectMessageField(window,request,read);
    if (![selected[@"fixture_id"] isEqual:@"body"]) fail(@"TEST_FAILED", @"Wrong message field selected");
    verifyExpectedFieldValue(selected,@"",read);
    fieldReject(@"FIELD_VALUE_CHANGED",^{ verifyExpectedFieldValue(selected,@"other",read); });
    fieldReject(@"FIELD_VALUE_CHANGED",^{ verifyExpectedFieldValue(@{},@"",read); });
    for (NSDictionary *change in @[@{@"field":@"Password"},@{@"value":@12},@{@"value":[NSNull null]},
                                  @{@"expected_recipient":@"2025550186"},@{@"extra":@YES},
                                  @{@"expected_url_prefix":@"https://voice.google.com.evil/u/0/"}]) {
        NSMutableDictionary *bad=request.mutableCopy; [bad addEntriesFromDictionary:change];
        fieldReject(@"INVALID_REQUEST",^{ validatedFieldRequest(bad); });
    }
    NSMutableDictionary *oversized=request.mutableCopy;
    oversized[@"value"]=[@"x" stringByPaddingToLength:1601 withString:@"x" startingAtIndex:0];
    fieldReject(@"INVALID_REQUEST",^{ validatedFieldRequest(oversized); });
    NSMutableDictionary *page=window[@"AXChildren"][0];
    NSURL *originalURL=page[@"AXURL"];
    for (NSString *bad in @[@"https://voice.google.com.evil/u/0/messages",@"http://voice.google.com/u/0/messages",
                           @"https://voice.google.com/u/1/messages",@"https://voice.google.com/u/0/calls",
                           @"https://other@voice.google.com/u/0/messages",@"https://voice.google.com:443/u/0/messages"]) {
        page[@"AXURL"]=[NSURL URLWithString:bad];
        fieldReject(@"FIELD_ORIGIN_UNVERIFIED",^{ selectMessageField(window,request,read); });
    }
    page[@"AXURL"]=originalURL;
    NSMutableDictionary *account=page[@"AXChildren"][0]; account[@"AXDescription"]=@"Other account";
    fieldReject(@"FIELD_ACCOUNT_UNVERIFIED",^{ selectMessageField(window,request,read); });
    account[@"AXDescription"]=request[@"expected_account_label"];
    NSMutableDictionary *details=page[@"AXChildren"][1]; NSMutableDictionary *selector=details[@"AXChildren"][0];
    NSMutableDictionary *row=selector[@"AXChildren"][0];
    row[@"AXTitle"]=@"(202) 555-0199 Remove (202) 555-0199";
    fieldReject(@"FIELD_RECIPIENT_UNVERIFIED",^{ selectMessageField(window,request,read); });
    row[@"AXTitle"]=@"(202) 555-0186 Remove (202) 555-0186";
    NSArray *cells=row[@"AXChildren"];
    row[@"AXChildren"]=@[@{@"AXRole":@"AXCell",@"AXTitle":@"(202) 555-0199"}];
    fieldReject(@"FIELD_RECIPIENT_UNVERIFIED",^{ selectMessageField(window,request,read); });
    row[@"AXChildren"]=cells;
    row[@"AXChildren"]=@[@{@"AXRole":@"AXCell",@"AXChildren":@[@{@"AXRole":@"AXGroup",@"AXChildren":@[@{@"AXRole":@"AXStaticText",@"AXValue":@"2 0 2 5 5 5 0 1 8 6"}]}]},
                         @{@"AXRole":@"AXCell",@"AXChildren":@[@{@"AXRole":@"AXButton",@"AXDescription":@"Remove 2 0 2 5 5 5 0 1 8 6?"}]}];
    if (selectMessageField(window,request,read)!=selected) fail(@"TEST_FAILED", @"An unnamed recipient cell with exact static text was not verified");
    row[@"AXChildren"]=@[@{@"AXRole":@"AXCell",@"AXChildren":@[@{@"AXRole":@"AXStaticText",@"AXValue":@"2 0 2 5 5 5 0 1 9 9"}]}];
    fieldReject(@"FIELD_RECIPIENT_UNVERIFIED",^{ selectMessageField(window,request,read); });
    row[@"AXChildren"]=@[@{@"AXRole":@"AXCell"}];
    fieldReject(@"FIELD_RECIPIENT_UNVERIFIED",^{ selectMessageField(window,request,read); });
    row[@"AXChildren"]=cells;
    selector[@"AXChildren"]=@[row,@{@"AXRole":@"AXRow",@"AXTitle":@"(202) 555-0199"}];
    fieldReject(@"FIELD_RECIPIENT_UNVERIFIED",^{ selectMessageField(window,request,read); });
    selector[@"AXChildren"]=@[row];
    NSArray *detailChildren=details[@"AXChildren"];
    details[@"AXChildren"]=[detailChildren arrayByAddingObject:@{@"AXRole":@"AXTextField",@"AXTitle":@"Type a message",@"AXValue":@"other"}];
    fieldReject(@"FIELD_NOT_UNIQUE",^{ selectMessageField(window,request,read); });
    details[@"AXChildren"]=detailChildren;
    // A field in an unrelated nested frame cannot create a candidate.
    page[@"AXChildren"]=[page[@"AXChildren"] arrayByAddingObject:@{@"AXRole":@"AXWebArea",@"AXURL":[NSURL URLWithString:@"https://unrelated.invalid"],
        @"AXChildren":@[@{@"AXRole":@"AXTextField",@"AXTitle":@"Type a message"}]}];
    if (selectMessageField(window,request,read)!=selected) fail(@"TEST_FAILED", @"A nested frame influenced field selection");
    NSArray *pageChildren=page[@"AXChildren"];
    page[@"AXChildren"]=[pageChildren arrayByAddingObject:account];
    if (selectMessageField(window,request,read)!=selected) fail(@"TEST_FAILED", @"A repeated reference was counted as a second account");
    page[@"AXChildren"]=pageChildren;
    details[@"AXChildren"]=[detailChildren arrayByAddingObject:[selected mutableCopy]];
    fieldReject(@"FIELD_NOT_UNIQUE",^{ selectMessageField(window,request,read); });
    details[@"AXChildren"]=detailChildren;
    NSMutableDictionary *cycle=[@{@"AXRole":@"AXGroup"} mutableCopy];
    cycle[@"AXChildren"]=@[cycle];
    fieldReject(@"FIELD_COVERAGE_INCOMPLETE",^{ completePageNodes(cycle,read); });
    [cycle removeObjectForKey:@"AXChildren"];
    NSMutableDictionary *entryRequest=request.mutableCopy;
    entryRequest[@"field"]=@"Type a name or phone number"; entryRequest[@"value"]=@"+12025550186";
    [entryRequest removeObjectForKey:@"expected_recipient"]; validatedFieldRequest(entryRequest);
    fieldReject(@"FIELD_RECIPIENT_UNVERIFIED",^{ selectMessageField(window,entryRequest,read); });
    selector[@"AXChildren"]=@[];
    NSDictionary *entry=@{@"AXRole":@"AXTextField",@"AXPlaceholderValue":@"Type a name or phone number",@"AXValue":@""};
    details[@"AXChildren"]=[detailChildren arrayByAddingObject:entry];
    if (selectMessageField(window,entryRequest,read)!=entry) fail(@"TEST_FAILED", @"The empty recipient entry field was not selected");
    details[@"AXChildren"]=@"incomplete";
    fieldReject(@"FIELD_COVERAGE_INCOMPLETE",^{ selectMessageField(window,entryRequest,read); });
    deadline=now()-1;
    fieldReject(@"TIMEOUT",^{ selectMessageField(window,entryRequest,read); });
    emit(@{@"ok":@YES,@"field_fixture_tests":@32,@"native_actions":@0,@"messages_sent":@0});
}
