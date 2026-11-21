"""M2-A: the agent definition schema and the built-in templates (pure unit tests: no database, no network)."""

import copy

import pytest
from pydantic import ValidationError

from agentplatform.schema import (
    BUILTIN_AGENT_KEYS,
    AgentDefinition,
    canonical,
    clean_note,
    content_hash,
    parse_definition,
    validate_slug,
)
from agentplatform.templates import get_template, list_templates

VALID = {
    "name": "Release Notes Writer",
    "description": "Turns a changelog into friendly release notes for customers.",
    "role": "Product marketer",
    "purpose": "Explain what changed and why it matters, without hype.",
    "instructions": "You write clear release notes from the changelog the user provides. Be accurate and concise.",
    "capabilities": ["release notes", "changelog summaries"],
    "allowed_tools": [],
    "input_expectations": "A changelog.",
    "output_expectations": "Markdown release notes.",
    "model": {"provider": "groq", "name": "llama-3.3-70b-versatile", "temperature": 0.5, "max_tokens": 1024},
    "metadata": {"category": "marketing"},
}


def make(**overrides):
    data = copy.deepcopy(VALID)
    data.update(overrides)
    return data


def invalid(**overrides):
    with pytest.raises(ValidationError) as info:
        AgentDefinition.model_validate(make(**overrides))
    # the error report WITHOUT the submitted values (pydantic's own str() echoes them; the API strips them, see test_agent_api)
    return str(info.value.errors(include_input=False, include_context=False, include_url=False))


# ------------------------------------------------------------------------------------------------ definition

def test_a_valid_definition_round_trips_as_plain_data():
    definition = AgentDefinition.model_validate(VALID)
    data = canonical(definition)
    assert data["name"] == "Release Notes Writer"
    assert data["model"] == {"provider": "groq", "name": "llama-3.3-70b-versatile", "temperature": 0.5, "max_tokens": 1024}
    assert parse_definition(data) == definition
    assert canonical(parse_definition(data)) == data
    with pytest.raises(ValidationError):
        definition.name = "changed"  # frozen


def test_defaults_and_minimal_definition():
    minimal = {k: VALID[k] for k in ("name", "description", "role", "purpose", "instructions")}
    minimal["model"] = {"provider": "groq", "name": "llama-3.3-70b-versatile"}
    definition = AgentDefinition.model_validate(minimal)
    assert definition.capabilities == [] and definition.allowed_tools == [] and definition.metadata == {}
    assert definition.model.temperature == 0.7 and definition.model.max_tokens is None


def test_missing_required_fields_are_rejected():
    for field in ("name", "description", "role", "purpose", "instructions", "model"):
        data = make()
        del data[field]
        with pytest.raises(ValidationError):
            AgentDefinition.model_validate(data)


def test_unknown_fields_are_rejected_everywhere():
    for extra in ({"user_id": "x"}, {"workspace_id": "x"}, {"tools": ["x"]}, {"python": "import os"}, {"api_key": "x"}):
        with pytest.raises(ValidationError):
            AgentDefinition.model_validate({**make(), **extra})
    data = make()
    data["model"]["base_url"] = "https://evil.example"
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(data)


def test_text_bounds_and_control_characters():
    assert "name" in invalid(name="x")
    assert "name" in invalid(name="x" * 81)
    assert "name" in invalid(name="two\nlines")
    assert "name" in invalid(name="bad\x00name")
    assert "instructions" in invalid(instructions="too short")
    assert "instructions" in invalid(instructions="x" * 8001)
    assert "instructions" in invalid(instructions="ok " * 10 + "\x07")
    assert "description" in invalid(description="short")
    invalid(name=5)
    invalid(instructions=None)
    # multi-line instructions with tabs and newlines are fine
    AgentDefinition.model_validate(make(instructions="Step one.\n\tStep two, indented.\nStep three is the last one."))


def test_probable_secrets_are_refused_without_echoing_them():
    secrets = [
        "Use the key sk-abcdefghijklmnopqrstuvwxyz123456 for access.",
        "gsk_abcdefghijklmnopqrstuvwxyz1234567890",
        "AKIAABCDEFGHIJKLMNOP is the access key.",
        "-----BEGIN RSA PRIVATE KEY----- MIIEow",
        "ghp_" + "a" * 36,
        "api_key = abcd1234efgh5678",
        "password: hunter2hunter2",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "postgres://user:s3cretpass@db.internal/app",
        "token eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJ4eXoifQ.c2lnbmF0dXJl",
    ]
    for secret in secrets:
        for field in ("instructions", "description", "purpose", "name", "input_expectations"):
            message = invalid(**{field: f"Normal prefix text. {secret}"})
            assert "credential" in message
            assert secret not in message and "hunter2" not in message and "s3cretpass" not in message
        with pytest.raises(ValidationError):
            AgentDefinition.model_validate(make(metadata={"note": secret}))
    # ordinary prose that merely mentions the words is fine
    AgentDefinition.model_validate(make(instructions="Explain how password managers and API keys work, in plain language."))


def test_capabilities_are_short_unique_lowercase_labels():
    assert canonical(AgentDefinition.model_validate(make(capabilities=["  Cold Email  ", "follow-ups"])))["capabilities"] == ["cold email", "follow-ups"]
    for bad in (["x"], ["a" * 41], ["Has/Slash"], ["dup", "dup"], ["ok", "OK"], [""], ["emoji \U0001F600"], ["a b"] * 13):
        invalid(capabilities=bad)


def test_allowed_tools_must_exist_and_no_tool_exists_yet():
    for tools in (["web_search"], ["shell"], ["python"], ["x"], ["a", "a"]):
        assert "tool" in invalid(allowed_tools=tools).lower()
    assert AgentDefinition.model_validate(make(allowed_tools=[])).allowed_tools == []


def test_model_configuration_is_bounded_and_provider_is_allow_listed():
    for model in (
        {"provider": "openai", "name": "gpt"}, {"provider": "groq"}, {"provider": "groq", "name": ""}, {"provider": "groq", "name": "bad name"},
        {"provider": "groq", "name": "m", "temperature": -0.1}, {"provider": "groq", "name": "m", "temperature": 2.1},
        {"provider": "groq", "name": "m", "temperature": "hot"}, {"provider": "groq", "name": "m", "temperature": True},
        {"provider": "groq", "name": "m", "max_tokens": 0}, {"provider": "groq", "name": "m", "max_tokens": 8193},
        {"provider": "groq", "name": "m", "max_tokens": 1.5}, {"provider": "groq", "name": "m", "max_tokens": True},
        {"provider": "groq", "name": "m" * 101},
    ):
        invalid(model=model)
    ok = AgentDefinition.model_validate(make(model={"provider": "groq", "name": "m", "temperature": 0, "max_tokens": 8192}))
    assert ok.model.temperature == 0.0 and ok.model.max_tokens == 8192


def test_metadata_is_bounded_and_credential_like_keys_are_refused():
    for bad in ({"Bad": "x"}, {"": "x"}, {"api_key": "x"}, {"password": "x"}, {"secret_note": "x"}, {"auth_token": "x"},
                {f"k{i}": "v" for i in range(11)}, {"ok": "v" * 201}, {"ok": 5}):
        invalid(metadata=bad)
    AgentDefinition.model_validate(make(metadata={"category": "sales", "owner.team": "growth"}))


def test_content_hash_is_stable_and_order_independent():
    a = AgentDefinition.model_validate(VALID)
    reordered = dict(reversed(list(VALID.items())))
    b = AgentDefinition.model_validate(reordered)
    assert content_hash(a) == content_hash(b) == content_hash(canonical(a))
    assert len(content_hash(a)) == 64
    assert content_hash(a) != content_hash(AgentDefinition.model_validate(make(role="Different role")))


def test_change_notes_are_cleaned_and_bounded():
    assert clean_note(None) is None and clean_note("   ") is None and clean_note(" fix typo ") == "fix typo"
    for bad in ("x" * 201, "bad\x00", "api_key = abcd1234efgh5678"):
        with pytest.raises(ValueError):
            clean_note(bad)


# ------------------------------------------------------------------------------------------------ slugs

def test_slug_rules():
    for good in ("abc", "my-agent", "sales-outreach-2", "a1b", "x" * 40):
        assert validate_slug(good) == good
    for bad in ("ab", "x" * 41, "-abc", "abc-", "1abc", "Abc", "a_b", "a b", "a--b", "", None, 5, "agents", "new", *BUILTIN_AGENT_KEYS):
        with pytest.raises(ValueError):
            validate_slug(bad)


# ------------------------------------------------------------------------------------------------ templates

def test_templates_are_stable_valid_and_few():
    templates = list_templates()
    assert [t.id for t in templates] == ["sales-outreach", "support-triage", "market-research-brief", "weekly-update-writer", "job-description-writer", "unit-economics-analyst", "launch-planner", "launch-comms"]
    assert all(t.version == 1 for t in templates)
    for template in templates:
        assert validate_slug(template.id) == template.id  # a template id is a valid default slug
        assert parse_definition(canonical(template.definition)) == template.definition  # revalidates
        if template.id in ("unit-economics-analyst", "launch-planner", "launch-comms"):  # the launch-readiness workforce: every tool it names is a REAL, runnable, read-only tool
            from agentplatform.tools import default_registry

            assert template.definition.allowed_tools and all(default_registry().executable(default_registry().resolve_pin(t)) for t in template.definition.allowed_tools)
        else:
            assert template.definition.allowed_tools == []  # the original templates use no tool, and none pretends to
        assert template.definition.model.provider == "groq"
        assert len(template.definition.instructions) > 400  # real instructions, not a stub
        assert "cannot browse the web" in template.definition.instructions  # honest about missing tools
    assert get_template("sales-outreach") is templates[0]
    assert get_template("nope") is None and get_template("") is None


def test_templates_are_immutable_and_instances_are_independent_copies():
    template = get_template("sales-outreach")
    before = content_hash(template.definition)
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        template.definition.name = "hacked"
    with pytest.raises(Exception):
        template.version = 2  # frozen dataclass
    copy_data = canonical(template.definition)
    copy_data["name"] = "Renamed copy"
    copy_data["capabilities"].append("extra")
    copy_data["metadata"]["x"] = "y"
    assert content_hash(template.definition) == before
    assert template.definition.name == "Sales Outreach Writer"
    assert "extra" not in template.definition.capabilities
    assert list_templates()[0].definition.model_copy(deep=True) == template.definition
