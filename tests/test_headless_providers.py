"""Provider changes the headless API depends on (HEADLESS_RESEARCH_API.md §3.8).

Against a fake OpenAI client (no network):
- streams ask for a usage chunk and report it; the tool loop does too;
- complete_json sends response_format json_object and reports usage;
- both options can be turned off for a server that rejects them;
- a consumer that stops reading closes the HTTP stream (the model stops);
- usage collected in one context is not seen by another."""
from types import SimpleNamespace as NS

from convert_search_ai.providers.base import collect_usage, report_usage
from convert_search_ai.providers.chat import OpenAICompatibleChatProvider


def chunk(text=None, usage=None):
    choices = [NS(delta=NS(content=text, tool_calls=None))] if text is not None else []
    return NS(choices=choices, usage=usage)


class Stream:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True


class Completions:
    def __init__(self, reply):
        self.calls, self.reply, self.streams = [], reply, []

    def create(self, **kw):
        self.calls.append(kw)
        if kw.get("stream"):
            s = Stream([chunk("Hel"), chunk("lo"),
                        chunk(usage=NS(prompt_tokens=11, completion_tokens=3))])
            self.streams.append(s)
            return s
        return NS(choices=[NS(message=NS(content=self.reply))],
                  usage=NS(prompt_tokens=40, completion_tokens=8))


def provider(**kw):
    p = OpenAICompatibleChatProvider(model="m", **kw)
    comp = Completions('{"a": 1}')
    p._client = lambda: NS(chat=NS(completions=comp))
    return p, comp


def test_stream_asks_for_and_reports_usage():
    p, comp = provider()
    seen = []
    with collect_usage(lambda i, o: seen.append((i, o))):
        assert "".join(p.stream([{"role": "user", "content": "hi"}])) == "Hello"
    assert comp.calls[0]["stream_options"] == {"include_usage": True}
    assert seen == [(11, 3)]


def test_tool_loop_reports_usage_too():
    p, comp = provider()
    seen = []
    with collect_usage(lambda i, o: seen.append((i, o))):
        list(p.run_tools([{"role": "user", "content": "hi"}], tools=[], execute=None))
    assert seen == [(11, 3)] and comp.calls[0]["stream_options"] == {"include_usage": True}


def test_complete_json_uses_json_mode_and_reports_usage():
    p, comp = provider()
    seen = []
    with collect_usage(lambda i, o: seen.append((i, o))):
        assert p.complete_json([{"role": "user", "content": "x"}], system="s") == '{"a": 1}'
    assert comp.calls[0]["response_format"] == {"type": "json_object"}
    assert comp.calls[0]["messages"][0] == {"role": "system", "content": "s"}
    assert seen == [(40, 8)]


def test_options_can_be_turned_off_for_servers_that_reject_them():
    p, comp = provider(stream_usage=False, json_mode=False)
    "".join(p.stream([{"role": "user", "content": "hi"}]))
    p.complete_json([{"role": "user", "content": "x"}])
    assert "stream_options" not in comp.calls[0] and "response_format" not in comp.calls[1]


def test_a_consumer_that_stops_early_closes_the_stream():
    p, comp = provider()
    gen = p.stream([{"role": "user", "content": "hi"}])
    assert next(gen) == "Hel"
    gen.close()
    assert comp.streams[0].closed is True


def test_usage_outside_a_collector_goes_nowhere_and_collectors_do_not_mix():
    report_usage(5, 5)                                  # no sink: ignored
    a, b = [], []
    with collect_usage(lambda i, o: a.append(i)):
        report_usage(1, 0)
        with collect_usage(lambda i, o: b.append(i)):
            report_usage(2, 0)
        report_usage(3, 0)
    assert a == [1, 3] and b == [2]
