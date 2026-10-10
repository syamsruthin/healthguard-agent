from __future__ import annotations

import pytest

from backend.agents.tools.sandbox import SandboxError, validate_code

ESCAPES = [
    "import os\nresult = os.listdir('/')",
    "result = ().__class__",
    "result = open('/etc/passwd').read()",
    "result = pd.read_csv('/etc/passwd')",
    "providers.to_csv('/tmp/x.csv')\nresult = 1",
    "result = getattr(providers, 'shape')",
    "result = providers.__class__.__bases__",
    "result = providers.query('specialty == \"Cardiology\"')",
]


@pytest.mark.parametrize("code", ESCAPES)
def test_escape_attempts_blocked(code: str) -> None:
    with pytest.raises(SandboxError):
        validate_code(code)


def test_valid_pandas_code_passes() -> None:
    code = (
        "df = providers[providers['specialty'] == 'Cardiology']\n"
        "result = df[['name', 'city']].head(5).to_dict('records')"
    )
    assert validate_code(code)


def test_result_assignment_required() -> None:
    with pytest.raises(SandboxError, match="result"):
        validate_code("x = 1")


def test_syntax_error_reported() -> None:
    with pytest.raises(SandboxError, match="syntax"):
        validate_code("result = (")
