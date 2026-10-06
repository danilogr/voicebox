import pytest

from speak.transcribe.lock import TranscriptionBusy, transcription_lock

ALIVE = {100, 200, 300}


def lock(tmp_path, pid):
    return transcription_lock(tmp_path, is_alive=lambda p: p in ALIVE, pid=pid)


def test_a_second_live_process_is_refused(tmp_path):
    lock(tmp_path, 100).acquire()
    with pytest.raises(TranscriptionBusy) as refused:
        lock(tmp_path, 200).acquire()
    assert refused.value.owner == 100


def test_the_process_that_claimed_it_is_refused_a_second_claim(tmp_path):
    """The transcription lock is claimed on a worker's behalf, so our own pid in the
    file means a transcription is starting - not that this call already holds it."""
    lock(tmp_path, 100).acquire()
    with pytest.raises(TranscriptionBusy) as refused:
        lock(tmp_path, 100).acquire()
    assert refused.value.owner == 100


def test_a_lock_left_by_a_dead_process_is_reclaimed(tmp_path):
    lock(tmp_path, 999).acquire()
    lock(tmp_path, 200).acquire()
    assert lock(tmp_path, 200).owner() == 200


def test_the_worker_takes_the_lock_over_from_the_process_that_claimed_it(tmp_path):
    lock(tmp_path, 100).acquire()
    lock(tmp_path, 200).take_over(previous=100)
    assert lock(tmp_path, 200).owner() == 200


def test_after_a_takeover_only_the_worker_can_release(tmp_path):
    claimer, worker = lock(tmp_path, 100), lock(tmp_path, 200)
    claimer.acquire()
    worker.take_over(previous=100)
    claimer.release()
    assert worker.owner() == 200
    worker.release()
    assert worker.owner() is None


def test_a_takeover_is_refused_when_a_third_live_process_holds_it(tmp_path):
    lock(tmp_path, 300).acquire()
    with pytest.raises(TranscriptionBusy):
        lock(tmp_path, 200).take_over(previous=100)


def test_a_worker_whose_claimer_died_still_takes_over(tmp_path):
    lock(tmp_path, 999).acquire()
    lock(tmp_path, 200).take_over(previous=999)
    assert lock(tmp_path, 200).owner() == 200
