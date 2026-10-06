"""One holder at a time across processes, elected by an atomic `mkdir`."""

import pytest

from speak.exclusive import ExclusiveLock

ALIVE = {100, 200, 300}


class BusyError(RuntimeError):
    def __init__(self, owner: int) -> None:
        super().__init__(f"held by {owner}")
        self.owner = owner


def lock(tmp_path, pid, *, reentrant=True):
    return ExclusiveLock(
        directory=tmp_path / "held",
        is_alive=lambda p: p in ALIVE,
        busy=BusyError,
        pid=pid,
        reentrant=reentrant,
    )


def test_a_second_live_process_is_refused(tmp_path):
    lock(tmp_path, 100).acquire()
    with pytest.raises(BusyError) as refused:
        lock(tmp_path, 200).acquire()
    assert refused.value.owner == 100


def test_the_holder_may_claim_again_when_reentrant(tmp_path):
    lock(tmp_path, 100).acquire()
    lock(tmp_path, 100).acquire()
    assert lock(tmp_path, 100).owner() == 100


def test_the_holder_is_refused_a_second_claim_when_not_reentrant(tmp_path):
    lock(tmp_path, 100, reentrant=False).acquire()
    with pytest.raises(BusyError):
        lock(tmp_path, 100, reentrant=False).acquire()


def test_a_lock_left_by_a_dead_process_is_reclaimed(tmp_path):
    lock(tmp_path, 999).acquire()
    lock(tmp_path, 200).acquire()
    assert lock(tmp_path, 200).owner() == 200


def test_only_the_holder_releases(tmp_path):
    lock(tmp_path, 100).acquire()
    lock(tmp_path, 200).release()
    assert lock(tmp_path, 100).owner() == 100
    lock(tmp_path, 100).release()
    assert lock(tmp_path, 100).owner() is None


def test_a_takeover_moves_the_lock_to_the_receiver(tmp_path):
    lock(tmp_path, 100).acquire()
    lock(tmp_path, 200).take_over(previous=100)
    assert lock(tmp_path, 200).owner() == 200


def test_the_context_manager_releases(tmp_path):
    with lock(tmp_path, 100):
        assert lock(tmp_path, 100).owner() == 100
    assert lock(tmp_path, 100).owner() is None
