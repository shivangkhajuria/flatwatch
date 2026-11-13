import asyncio

from flatwatch.backtest import LABEL, LabelledNotifier


class TG:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, text, reply_markup=None, silent=False):
        self.sent.append(text)
        return {"message_id": 1}

    async def edit_message_text(self, message_id, text, reply_markup=None):
        self.edits.append(text)
        return {"message_id": message_id}


def test_every_backtest_message_is_labelled():
    async def go():
        tg = TG()
        n = LabelledNotifier(tg)
        await n.send("alert")
        await n.edit(1, "edited")
        assert tg.sent == [LABEL + "alert"] and tg.edits == [LABEL + "edited"]
    asyncio.run(go())
