from app.services.inbound_guard import looks_like_automated_agent
from app.services.whatsapp import _blocked_test_recipient


def test_known_test_numbers_are_blocked():
    assert _blocked_test_recipient("9876543210")
    assert _blocked_test_recipient("+919999900201")
    assert _blocked_test_recipient("919999900291")


def test_other_numbers_are_not_blocked():
    assert not _blocked_test_recipient("9876543211")
    assert not _blocked_test_recipient("+919999900299")


def test_automated_agent_detector_is_conservative():
    assert looks_like_automated_agent("Hello, I am an AI assistant for this business.")
    assert looks_like_automated_agent("This is an automated message. Please reply STOP.")
    assert looks_like_automated_agent("I am a bot and can help you with your order.")
    assert not looks_like_automated_agent("Hi, I want to know your laundry price.")
    assert not looks_like_automated_agent("Do you have pickup service?")
