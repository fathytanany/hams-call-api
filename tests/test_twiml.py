import pytest

from callapi.twiml import Stream, TwimlError, parse

OK = (
    '<Response><Connect><Stream url="wss://app.example/s" name="n">'
    '<Parameter name="mode" value="slow"/><Parameter name="empty" value=""/>'
    "</Stream></Connect></Response>"
)


def test_parses_stream_url_and_parameters() -> None:
    assert parse(OK) == Stream("wss://app.example/s", {"mode": "slow", "empty": ""})


def test_ws_without_parameters_and_with_whitespace() -> None:
    twiml = """<?xml version="1.0"?>
    <Response>
      <Connect><Stream url="ws://localhost:9000/stream"/></Connect>
    </Response>"""
    assert parse(twiml) == Stream("ws://localhost:9000/stream", {})


@pytest.mark.parametrize(
    ("twiml", "fragment"),
    [
        ("<Response>", "not valid XML"),
        ("<Foo/>", "root must be <Response>"),
        ("<Response><Say>hi</Say></Response>", "exactly one <Connect>"),
        ("<Response><Connect/></Response>", "exactly one <Stream>"),
        (
            '<Response><Connect><Stream url="wss://a/s"/></Connect><Hangup/></Response>',
            "exactly one <Connect>",
        ),
        ('<Response><Connect><Stream url="https://a/s"/></Connect></Response>', "ws://"),
        ("<Response><Connect><Stream/></Connect></Response>", "ws://"),
        ('<Response><Connect><Stream url="wss://a/s?x=1"/></Connect></Response>', "query"),
        (
            '<Response><Connect><Stream url="wss://a/s" track="both"/></Connect></Response>',
            "Unsupported <Stream> attribute(s): track",
        ),
        (
            '<Response><Connect><Stream url="wss://a/s"><Foo/></Stream></Connect></Response>',
            "only contain <Parameter>",
        ),
        (
            '<Response><Connect><Stream url="wss://a/s"><Parameter name="x"/>'
            "</Stream></Connect></Response>",
            "name and value",
        ),
        (
            f'<Response><Connect><Stream url="wss://a/s"><Parameter name="x" value="{"v" * 499}"/>'
            "</Stream></Connect></Response>",
            "under 500",
        ),
        ("<Response>" + " " * 4000 + "</Response>", "at most 4000"),
    ],
)
def test_rejects(twiml: str, fragment: str) -> None:
    with pytest.raises(TwimlError) as e:
        parse(twiml)
    assert fragment in str(e.value)


def test_entity_expansion_is_refused() -> None:
    bomb = (
        '<!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
        '<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;"><!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">'
        '<!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;"><!ENTITY h "&g;&g;&g;&g;&g;&g;&g;&g;&g;&g;">]>'
        "<Response>&h;</Response>"
    )
    with pytest.raises(TwimlError, match="not valid XML"):
        parse(bomb)
