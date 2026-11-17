"""Transcript state used by the overlay.

The backend does not put ``speaker`` on transcript events (only on the REST
transcript), so finals start as ``candidate`` and are relabeled when the
authoritative transcript is fetched. Turns are deduplicated by ``turn_id`` and
``segment_id`` because a reconnect can replay finals.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional

SPEAKER_CANDIDATE = "candidate"
SPEAKER_INTERVIEWER = "interviewer"
SPEAKER_ASSISTANT = "assistant"


@dataclass(frozen=True)
class Turn:
    turn_id: str
    text: str
    sequence_number: int
    speaker: str = SPEAKER_CANDIDATE

    @property
    def label(self) -> str:
        return {
            SPEAKER_INTERVIEWER: "Interviewer",
            SPEAKER_ASSISTANT: "Assistant",
            SPEAKER_CANDIDATE: "You",
        }.get(self.speaker, self.speaker.title())


class TranscriptStore:
    def __init__(self) -> None:
        self._turns: List[Turn] = []
        self._turn_ids: set = set()
        self._segment_ids: set = set()
        self._partial_by_segment: Dict[str, str] = {}
        self._partial_without_segment: str = ""

    # ------------------------------------------------------------------ reads
    @property
    def turns(self) -> List[Turn]:
        return list(self._turns)

    @property
    def partial_text(self) -> str:
        if not self._partial_by_segment:
            return self._partial_without_segment
        # Show the most recently updated segment as the active partial.
        return next(reversed(self._partial_by_segment.values()))

    @property
    def latest_turn_id(self) -> Optional[str]:
        return self._turns[-1].turn_id if self._turns else None

    @property
    def latest_speaker(self) -> Optional[str]:
        return self._turns[-1].speaker if self._turns else None

    def candidate_turn_id(self) -> Optional[str]:
        for turn in reversed(self._turns):
            if turn.speaker == SPEAKER_CANDIDATE:
                return turn.turn_id
        return None

    def rendered_turns(self) -> List[tuple]:
        return [(turn.label, turn.text) for turn in self._turns]

    # ------------------------------------------------------------------ writes
    def apply_delta(self, payload: Dict[str, Any]) -> None:
        text = str(payload.get("text") or "")
        segment_id = payload.get("segment_id")
        if segment_id:
            self._partial_by_segment[str(segment_id)] = text
        else:
            self._partial_without_segment = text

    def apply_final(self, payload: Dict[str, Any]) -> Optional[Turn]:
        """Add a final turn. Returns ``None`` when it was a replay/duplicate."""
        turn_id = str(payload.get("turn_id") or "")
        segment_id = payload.get("segment_id")
        if turn_id and turn_id in self._turn_ids:
            return None
        if segment_id and str(segment_id) in self._segment_ids:
            return None

        sequence = payload.get("sequence_number")
        turn = Turn(
            turn_id=turn_id or f"segment:{segment_id}",
            text=str(payload.get("text") or ""),
            sequence_number=sequence if isinstance(sequence, int) else len(self._turns) + 1,
        )
        carried_partials = dict(self._partial_by_segment)
        self._turns.append(turn)
        if turn_id:
            self._turn_ids.add(turn_id)
        if segment_id:
            self._segment_ids.add(str(segment_id))
            carried_partials.pop(str(segment_id), None)
        else:
            # A final without a segment id closes whatever partial was live.
            carried_partials.clear()
        self._partial_by_segment = carried_partials
        self._partial_without_segment = ""

        # Keep newest-last ordering even if a replay arrives out of order.
        if len(self._turns) > 1 and self._turns[-2].sequence_number > turn.sequence_number:
            self._turns.sort(key=lambda item: item.sequence_number)
        return turn

    def merge_authoritative(self, turns: Iterable[Dict[str, Any]]) -> bool:
        """Merge ``GET /sessions/{id}/transcript`` into current state.

        Returns ``True`` when the visible turns changed. This is the only path
        that knows the real speaker, and it also repairs any gap after
        ``system.replay_gap``.

        The REST list is merged rather than replacing local state: it can lag
        the event stream by a moment, and dropping a just-rendered final would
        make the overlay flicker (or erase the turn entirely on an empty read).
        Turns are immutable once final, so the union is safe.
        """
        merged: Dict[str, Turn] = {turn.turn_id: turn for turn in self._turns}
        for item in turns:
            turn_id = str(item.get("id") or "")
            if not turn_id:
                continue
            sequence = item.get("sequence_number")
            merged[turn_id] = Turn(
                turn_id=turn_id,
                text=str(item.get("text") or ""),
                sequence_number=sequence if isinstance(sequence, int) else 0,
                speaker=str(item.get("speaker") or SPEAKER_CANDIDATE),
            )

        ordered = sorted(merged.values(), key=lambda item: item.sequence_number)
        changed = ordered != self._turns
        self._turns = ordered
        self._turn_ids = set(merged)
        return changed

    def relabel_speakers(self, mapping: Dict[str, str]) -> bool:
        """Apply ``turn_id -> speaker`` without discarding unknown turns."""
        changed = False
        updated = []
        for turn in self._turns:
            speaker = mapping.get(turn.turn_id)
            if speaker and speaker != turn.speaker:
                updated.append(replace(turn, speaker=speaker))
                changed = True
            else:
                updated.append(turn)
        if changed:
            self._turns = updated
        return changed

    def clear_partial(self) -> None:
        self._partial_by_segment.clear()
        self._partial_without_segment = ""
