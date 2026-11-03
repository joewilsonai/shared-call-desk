import pytest
from audio_policy import (KEEP_VIRTUAL,RESTORE_ORIGINAL,audio_cleanup_policy,
                          lease_released,release_fields)


def test_policy_is_explicit_opt_in_and_rejects_typos():
    assert audio_cleanup_policy({})==RESTORE_ORIGINAL
    assert audio_cleanup_policy({'audio_cleanup_policy':KEEP_VIRTUAL})==KEEP_VIRTUAL
    with pytest.raises(ValueError):audio_cleanup_policy({'audio_cleanup_policy':'keep-anything'})


def test_retained_defaults_are_released_not_falsely_restored():
    lease=release_fields(KEEP_VIRTUAL,'Observed virtual defaults retained',123)
    assert lease_released(lease)
    assert lease['status']=='released' and lease['routes_restored'] is False
    assert 'restored_at' not in lease and 'restoration_evidence' not in lease
    assert lease_released({'status':'restored'})  # Historical records remain usable.


@pytest.mark.parametrize('field',['release_kind','audio_cleanup_policy','audio_cleanup_verified','audio_cleanup_evidence','routes_restored'])
def test_released_word_alone_is_not_cleanup_proof(field):
    lease=release_fields(KEEP_VIRTUAL,'Observed virtual defaults retained',123)
    lease.pop(field)
    assert not lease_released(lease)
