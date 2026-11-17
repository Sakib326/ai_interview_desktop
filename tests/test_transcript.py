from desktop_client.transcript import (
    SPEAKER_INTERVIEWER,
    TranscriptStore,
)


def test_apply_delta_replaces_partial_per_segment():
    store = TranscriptStore()
    store.apply_delta({"text": "I worked", "segment_id": "s1"})
    assert store.partial_text == "I worked"
    store.apply_delta({"text": "I worked on payments", "segment_id": "s1"})
    assert store.partial_text == "I worked on payments"


def test_apply_final_dedupes_by_turn_and_segment():
    store = TranscriptStore()
    payload = {
        "text": "Hello.",
        "turn_id": "t1",
        "segment_id": "s1",
        "sequence_number": 1,
    }
    assert store.apply_final(payload) is not None
    assert store.apply_final(payload) is None
    # A duplicate segment under a different turn id is still a replay.
    assert store.apply_final({**payload, "turn_id": "t2"}) is None
    assert len(store.turns) == 1
    assert store.partial_text == ""


def test_final_clears_partial_and_orders_by_sequence():
    store = TranscriptStore()
    store.apply_delta({"text": "partial", "segment_id": "s1"})
    store.apply_final({"text": "second", "turn_id": "t2", "sequence_number": 2})
    store.apply_final({"text": "first", "turn_id": "t1", "sequence_number": 1})
    assert [turn.text for turn in store.turns] == ["first", "second"]
    assert store.partial_text == ""


def test_candidate_turn_id_skips_interviewer():
    store = TranscriptStore()
    store.apply_final(
        {"text": "question", "turn_id": "t1", "sequence_number": 1}
    )
    store.merge_authoritative(
        [
            {
                "id": "t1",
                "text": "question",
                "sequence_number": 1,
                "speaker": SPEAKER_INTERVIEWER,
            },
            {
                "id": "t2",
                "text": "answer",
                "sequence_number": 2,
                "speaker": "candidate",
            },
        ]
    )
    assert store.candidate_turn_id() == "t2"
    assert store.latest_turn_id == "t2"
    assert store.rendered_turns() == [
        ("Interviewer", "question"),
        ("You", "answer"),
    ]


def test_merge_authoritative_reports_change_and_dedupes():
    store = TranscriptStore()
    store.apply_final({"text": "one", "turn_id": "t1", "sequence_number": 1})
    changed = store.merge_authoritative(
        [
            {"id": "t1", "text": "one", "sequence_number": 1, "speaker": "candidate"},
            {"id": "t1", "text": "one", "sequence_number": 1, "speaker": "candidate"},
        ]
    )
    assert changed is False  # same visible turns
    assert len(store.turns) == 1

    assert store.merge_authoritative(
        [
            {"id": "t1", "text": "one", "sequence_number": 1, "speaker": "candidate"},
            {"id": "t2", "text": "two", "sequence_number": 2, "speaker": "interviewer"},
        ]
    )
    assert len(store.turns) == 2


def test_relabel_speakers_keeps_unknown_turns():
    store = TranscriptStore()
    store.apply_final({"text": "a", "turn_id": "t1", "sequence_number": 1})
    store.apply_final({"text": "b", "turn_id": "t2", "sequence_number": 2})
    assert store.relabel_speakers({"t1": "interviewer"}) is True
    assert store.turns[0].speaker == "interviewer"
    assert store.turns[1].speaker == "candidate"
