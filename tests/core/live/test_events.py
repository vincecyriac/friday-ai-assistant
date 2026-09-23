import dataclasses

import pytest

from friday.core.live import (AudioOut, Connected, Disconnected, GoAway, Interrupted, TextOut,
                              ToolFinished, ToolStarted, Transcript, TurnComplete)


def test_events_are_frozen_value_objects():
    assert Connected(resumed=True) == Connected(resumed=True)
    assert AudioOut(pcm=b"ab").pcm == b"ab"
    assert TextOut(text="hi").text == "hi"
    assert Transcript(text="yes", role="user", final=True).final is True
    assert Interrupted() == Interrupted() and TurnComplete() == TurnComplete()
    assert ToolStarted(name="t", args={"a": 1}).args == {"a": 1}
    assert ToolFinished(name="t", output="o", ms=5, failed=False).failed is False
    assert GoAway(time_left=None).time_left is None
    assert Disconnected(reason="closed", will_retry=False).will_retry is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        Connected(resumed=True).resumed = False
