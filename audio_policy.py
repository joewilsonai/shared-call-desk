"""Configured Google Voice routing outcomes, independent of worker audio I/O."""
RESTORE_ORIGINAL='restore_original'
KEEP_VIRTUAL='keep_virtual'
VIRTUAL_ROUTES={'Microphone':'Elle Call Mic (Virtual)','Speakers':'Google Voice Return (Virtual)'}


def audio_cleanup_policy(record):
    value=record.get('audio_cleanup_policy',RESTORE_ORIGINAL)
    if value not in (RESTORE_ORIGINAL,KEEP_VIRTUAL):
        raise ValueError('audio_cleanup_policy must be restore_original or keep_virtual')
    return value


def lease_released(lease):
    if not isinstance(lease,dict):return False
    if lease.get('status')=='restored':return True  # Historical schema.
    return (lease.get('status')=='released'
            and lease.get('release_kind')=='persistent_virtual_defaults'
            and lease.get('audio_cleanup_policy')==KEEP_VIRTUAL
            and lease.get('audio_cleanup_verified') is True
            and lease.get('routes_restored') is False
            and isinstance(lease.get('audio_cleanup_evidence'),str)
            and bool(lease['audio_cleanup_evidence'].strip()))


def cleanup_complete(receipt):
    return receipt.get('released') is True or receipt.get('restored') is True


def release_fields(policy,evidence,at):
    common={'audio_cleanup_policy':policy,'audio_cleanup_verified':True,
            'audio_cleanup_evidence':evidence,'routes_restored':policy==RESTORE_ORIGINAL}
    if policy==KEEP_VIRTUAL:
        return dict(common,status='released',release_kind='persistent_virtual_defaults',released_at=at)
    return dict(common,status='restored',restored_at=at,restoration_evidence=evidence)


def cleanup_instruction(policy):
    if policy==KEEP_VIRTUAL:
        return 'Verify the configured virtual microphone and speaker remain selected; keep them as the Google Voice defaults.'
    return 'Restore and verify the microphone and speaker selections saved before the call.'
