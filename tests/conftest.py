# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Turn a missing grammar into a failure when CI asks for it.

`tests/test_language_plan.py` skips a language whose grammar is not
installed, which keeps the suite runnable on a checkout without the
grammars.  The cost is that CI went green for a month while the
JavaScript plan was never executed once: the grammar was not a declared
dependency, so the test skipped, and a skip is not a pass.  With
KEN_STRICT_GRAMMARS set, a skip for a missing grammar is a failure, and
an undeclared grammar breaks the build instead of quietly shrinking it.
"""
from __future__ import annotations

import os

import pytest

STRICT = os.environ.get("KEN_STRICT_GRAMMARS", "") not in ("", "0")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if STRICT and report.skipped:
        reason = str(report.longrepr)
        if "grammar not installed" in reason:
            report.outcome = "failed"
            report.longrepr = (
                f"strict grammars: {item.nodeid} skipped because a grammar is "
                "missing; declare it in pyproject.toml or drop the case:\n"
                f"    {reason.rsplit(chr(10), 1)[-1].strip()}"
            )
        elif "sysmlpy not installed" in reason:
            pass  # declared test dependency, not a grammar; skips on purpose
    return report
