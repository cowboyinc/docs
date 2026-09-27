import json

from cowboy_sdk import actor, capture, codec, runner, runtime, public, Address


SYSTEM_PROMPT = "You are a concise proposal reviewer. Return a practical review memo in one short paragraph."


@actor
class CapstoneAgent:
    def _ensure_state(self):
        if self.storage.get("owner") is None:
            self.storage["owner"] = self.sender.to_hex(checksum=False)
        if self.storage.get("admins") is None:
            self.storage["admins"] = {}
        if self.storage.get("proposal_count") is None:
            self.storage["proposal_count"] = 0
        if self.storage.get("approved_count") is None:
            self.storage["approved_count"] = 0
        if self.storage.get("rejected_count") is None:
            self.storage["rejected_count"] = 0

    def _is_admin(self):
        self._ensure_state()
        sender = self.sender.to_hex(checksum=False)
        if sender == self.storage.get("owner", ""):
            return True
        admins = dict(self.storage.get("admins") or {})
        return bool(admins.get(sender))

    def _proposal_key(self, proposal_id, field):
        return "proposal:" + str(int(proposal_id)) + ":" + field

    def _read_proposal(self, proposal_id):
        return {
            "proposal_id": int(proposal_id),
            "title": self.storage.get(self._proposal_key(proposal_id, "title"), ""),
            "body": self.storage.get(self._proposal_key(proposal_id, "body"), ""),
            "author": self.storage.get(self._proposal_key(proposal_id, "author"), ""),
            "status": self.storage.get(self._proposal_key(proposal_id, "status"), "missing"),
            "review_status": self.storage.get(self._proposal_key(proposal_id, "review_status"), "none"),
            "review_memo": self.storage.get(self._proposal_key(proposal_id, "review_memo"), ""),
        }

    def init(self, payload):
        self.storage["__name__"] = "Capstone Proposal Agent"
        self.storage["__description__"] = "Proposal workflow with permissions and runner.llm review"
        self.storage["__version__"] = "1.0.0"
        data = json.loads(payload) if payload else {}
        raw_owner = data.get("owner", "")
        self.storage["owner"] = Address(raw_owner).to_hex(checksum=False) if raw_owner else ""
        if not self.storage["owner"]:
            self.storage["owner"] = self.sender.to_hex(checksum=False)
        self.storage["admins"] = {}
        self.storage["proposal_count"] = 0
        self.storage["approved_count"] = 0
        self.storage["rejected_count"] = 0
        runtime.emit_event("capstone.init", {"owner": self.storage["owner"]})
        return codec.encode({"status": "initialized", "owner": self.storage["owner"]})

    @public(decode="cbor")
    def submit_proposal(self, payload):
        self._ensure_state()
        data = payload or {}
        proposal_id = int(self.storage.get("proposal_count", 0)) + 1
        self.storage["proposal_count"] = proposal_id
        self.storage[self._proposal_key(proposal_id, "title")] = str(data.get("title", "Untitled proposal"))
        self.storage[self._proposal_key(proposal_id, "body")] = str(data.get("body", ""))
        self.storage[self._proposal_key(proposal_id, "author")] = self.sender.to_hex(checksum=False)
        self.storage[self._proposal_key(proposal_id, "status")] = "submitted"
        self.storage[self._proposal_key(proposal_id, "review_status")] = "none"
        self.storage[self._proposal_key(proposal_id, "review_memo")] = ""
        runtime.emit_event(
            "capstone.proposal_submitted",
            {"proposal_id": proposal_id, "title": self.storage[self._proposal_key(proposal_id, "title")]},
        )
        return {"status": "submitted", "proposal_id": proposal_id}

    # @public rides outside @runner.continuation so the tag lands on the compiled
    # entrypoint; without it the SDK's deny-by-default gate rejects external
    # calls. decode="raw" is the only codec a continuation may declare (the FSM
    # legs own their payload path), so the handler codec.decodes below.
    @public(decode="raw")
    @runner.continuation
    async def review_with_llm(self, payload):
        self._ensure_state()
        data = codec.decode(payload) if payload else {}
        proposal_id = int(data.get("proposal_id", self.storage.get("proposal_count", 0)))
        if proposal_id <= 0:
            return codec.encode({"error": "PROPOSAL_NOT_FOUND"})

        proposal = self._read_proposal(proposal_id)
        if proposal["status"] == "missing":
            return codec.encode({"error": "PROPOSAL_NOT_FOUND"})

        self.storage[self._proposal_key(proposal_id, "review_status")] = "pending"
        prompt = json.dumps(
            {
                "instruction": "Review this proposal for clarity, risk, and next action.",
                "title": proposal["title"],
                "body": proposal["body"],
            },
            sort_keys=True,
        )

        ctx = capture()
        ctx.proposal_id = proposal_id

        result = await runner.llm(
            prompt,
            system_prompt=SYSTEM_PROMPT,
            max_tokens=256,
            temperature=0.2,
        )

        proposal_id = ctx.proposal_id
        memo = result.text
        self.storage[self._proposal_key(proposal_id, "review_status")] = "done"
        self.storage[self._proposal_key(proposal_id, "review_memo")] = memo
        runtime.emit_event("capstone.review_ready", {"proposal_id": proposal_id, "memo": memo[:200]})
        return codec.encode({"status": "reviewed", "proposal_id": proposal_id})

    @public(decode="cbor")
    def approve(self, payload):
        if not self._is_admin():
            return {"error": "UNAUTHORIZED"}
        data = payload or {}
        proposal_id = int(data.get("proposal_id", 0))
        if proposal_id <= 0:
            return {"error": "PROPOSAL_NOT_FOUND"}
        self.storage[self._proposal_key(proposal_id, "status")] = "approved"
        self.storage["approved_count"] = int(self.storage.get("approved_count", 0)) + 1
        runtime.emit_event("capstone.approved", {"proposal_id": proposal_id})
        return {"status": "approved", "proposal_id": proposal_id}

    @public(decode="cbor")
    def reject(self, payload):
        if not self._is_admin():
            return {"error": "UNAUTHORIZED"}
        data = payload or {}
        proposal_id = int(data.get("proposal_id", 0))
        if proposal_id <= 0:
            return {"error": "PROPOSAL_NOT_FOUND"}
        self.storage[self._proposal_key(proposal_id, "status")] = "rejected"
        self.storage["rejected_count"] = int(self.storage.get("rejected_count", 0)) + 1
        runtime.emit_event("capstone.rejected", {"proposal_id": proposal_id})
        return {"status": "rejected", "proposal_id": proposal_id}

    @public(decode="cbor")
    def get_proposal(self, payload):
        self._ensure_state()
        data = payload or {}
        proposal_id = int(data.get("proposal_id", self.storage.get("proposal_count", 0)))
        return self._read_proposal(proposal_id)

    @public(decode="cbor")
    def get_stats(self, payload):
        self._ensure_state()
        return {
            "proposal_count": int(self.storage.get("proposal_count", 0)),
            "approved_count": int(self.storage.get("approved_count", 0)),
            "rejected_count": int(self.storage.get("rejected_count", 0)),
            "owner": self.storage.get("owner", ""),
        }


def _get_actor():
    return CapstoneAgent()


def init(payload):
    return _get_actor().init(payload)


def submit_proposal(payload):
    return _get_actor().submit_proposal(payload)


def review_with_llm(payload):
    return _get_actor().review_with_llm(payload)


def review_with_llm__resume(payload):
    return _get_actor().review_with_llm__resume(payload)


def approve(payload):
    return _get_actor().approve(payload)


def reject(payload):
    return _get_actor().reject(payload)


def get_proposal(payload):
    return _get_actor().get_proposal(payload)


def get_stats(payload):
    return _get_actor().get_stats(payload)
