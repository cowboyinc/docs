import json
from cowboy_sdk import Address, actor, deferred, pure, public, send, codec


@actor
class JobValidator:
    """Validator actor that approves or rejects submitted work."""

    def _ensure_state(self):
        if self.storage.get("verdicts") is None:
            self.storage["verdicts"] = {}
        if self.storage.get("owner") is None:
            self.storage["owner"] = self.sender.to_hex(checksum=False)

    @public(decode="raw")
    def init(self, payload):
        self.storage["__name__"] = "Job Validator"
        self.storage["__description__"] = "Multi-actor workflow: job validator"
        self.storage["__version__"] = "1.0.0"
        data = json.loads(payload) if payload else {}
        sender = self.sender.to_hex(checksum=False)
        self.storage["owner"] = Address(data.get("owner") or sender).to_hex(checksum=False)
        self.storage["verdicts"] = {}
        return codec.encode({"status": "initialized"})

    def _validate(self, data):
        self._ensure_state()
        job = dict(data.get("job") or {})
        submission = dict(data.get("submission") or {})
        submission_id = int(data.get("submission_id", submission.get("submission_id", 0)))
        settlement_address = data.get("settlement_address", "")

        required_keyword = str(job.get("required_keyword", "")).lower()
        work_text = str(submission.get("work_text", "")).lower()
        approved = bool(required_keyword and required_keyword in work_text)
        verdict = {
            "submission_id": submission_id,
            "job_id": int(job.get("job_id", 0)),
            "approved": approved,
            "reason": "keyword matched" if approved else "required keyword missing",
            "worker": submission.get("worker", ""),
            "reward_units": int(job.get("reward_units", 0)) if approved else 0,
        }
        verdicts = dict(self.storage.get("verdicts") or {})
        verdicts[submission_id] = verdict
        self.storage["verdicts"] = verdicts

        if settlement_address:
            send(
                settlement_address,
                codec.encode(verdict | {"settlement_address": settlement_address}),
                raw=True,
            )

        return {"status": "validated", **verdict}

    @deferred
    @public(decode="cbor")
    def validate_submission(self, payload):
        return self._validate(payload or {})

    @pure
    @public(decode="cbor")
    def get_verdict(self, payload):
        self._ensure_state()
        data = payload or {}
        submission_id = int(data.get("submission_id", 0))
        verdicts = dict(self.storage.get("verdicts") or {})
        return verdicts.get(submission_id, {"error": "VERDICT_NOT_FOUND"})

    @deferred
    @public(decode="cbor")
    def handle_message(self, payload):
        return self._validate(payload or {})


def _get_actor():
    return JobValidator()


def init(payload):
    return _get_actor().init(payload)


def validate_submission(payload):
    return _get_actor().validate_submission(payload)


def get_verdict(payload):
    return _get_actor().get_verdict(payload)


def handle_message(payload):
    return _get_actor().handle_message(payload)
