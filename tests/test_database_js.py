"""Поведение `static/database.js` под Node с управляемыми часами и таймерами.

Браузер в CI не поднимаем, но `node --check` проверяет лишь синтаксис. Здесь скрипт
исполняется целиком: подставлены форма, кнопка, `fetch`, `Date.now` и `setTimeout`,
и время двигается вручную. Так ловится дефект, из-за которого после 429 с
`Retry-After` больше часа кнопка не включалась никогда (ревью PR #40).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "src" / "sniffer" / "dashboard" / "static" / "database.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node не установлен")

# Минимальный DOM и управляемое время. Сценарий — JSON-список шагов, итог — JSON.
HARNESS = r"""
const fs = require("fs");
const [scriptPath, scenarioJson] = process.argv.slice(2);
const scenario = JSON.parse(scenarioJson);
let now = 1_700_000_000_000;
const timers = [];
const realDate = Date;
global.Date = class extends realDate {
  constructor(...a) { super(...(a.length ? a : [now])); }
  static now() { return now; }
};
global.setTimeout = (fn, ms) => {
  timers.push({ at: now + Math.max(0, ms), fn });
  return timers.length;
};
function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    const next = timers[0];
    if (!next || next.at > end) break;
    timers.shift();
    now = next.at;
    next.fn();
  }
  now = end;
}
const attrs = {};
const status = { textContent: "", className: "" };
const button = { disabled: false };
let submitHandler = null;
const row = { setAttribute: (k, v) => { attrs[k] = v; }, getAttribute: (k) => attrs[k] };
const form = {
  action: "/rejects/%40k/retry",
  querySelector: () => button,
  closest: () => row,
  parentElement: { querySelector: () => status },
  setAttribute: () => {}, removeAttribute: () => {},
  addEventListener: (_, fn) => { submitHandler = fn; },
};
global.document = { querySelectorAll: () => [form] };
global.FormData = function () {};
global.URLSearchParams = function () {};
let calls = 0;
let reply = null;
global.fetch = () => {
  calls += 1;
  if (reply.network) return Promise.reject(new TypeError("offline"));
  const headers = { get: (k) => (k === "Retry-After" ? reply.retryAfter ?? null : null) };
  return Promise.resolve({
    ok: reply.status < 300, status: reply.status, headers,
    text: () => Promise.resolve(JSON.stringify({ message: reply.message || "m" })),
  });
};
eval(fs.readFileSync(scriptPath, "utf8"));
const flush = () => new Promise((r) => setImmediate(r));
const snap = () => ({ disabled: button.disabled, state: attrs["data-retry-state"] || null,
                      status: status.textContent, calls });
(async () => {
  const out = [];
  for (const step of scenario) {
    if (step.reply) reply = step.reply;
    if (step.submit) {
      for (let i = 0; i < step.submit; i++) submitHandler({ preventDefault() {} });
      for (let i = 0; i < 5; i++) await flush();
    }
    if (step.advance) advance(step.advance);
    out.push(snap());
  }
  console.log(JSON.stringify(out));
})();
"""


def run(scenario: list[dict[str, object]], tmp_path: Path) -> list[dict[str, object]]:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 - фиксированный argv: node и свои файлы
        [NODE, str(harness), str(SCRIPT), json.dumps(scenario)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    result: list[dict[str, object]] = json.loads(done.stdout)
    return result


HOUR_MS = 3600 * 1000


@pytest.mark.parametrize("retry_after_s", [70, 2 * 3600, 23 * 3600])
def test_button_comes_back_after_a_long_429(retry_after_s: int, tmp_path: Path) -> None:
    """Лимит на 2 часа (и почти сутки) снимается сам, а не держит кнопку до перезагрузки."""
    wait_ms = retry_after_s * 1000
    steps = run(
        [
            {"reply": {"status": 429, "retryAfter": str(retry_after_s)}, "submit": 1},
            {"advance": wait_ms - 1000},
            {"advance": 61_000},
        ],
        tmp_path,
    )
    after_refusal, just_before, after = steps
    assert after_refusal["disabled"] is True
    assert after_refusal["state"] == "limited"
    assert "Кнопка включится сама около" in str(after_refusal["status"])
    assert just_before["disabled"] is True
    assert after["disabled"] is False
    assert after["state"] == "ready"
    assert after["status"] == "Лимит прошёл — можно повторить."


def test_429_without_retry_after_says_what_to_do(tmp_path: Path) -> None:
    steps = run(
        [{"reply": {"status": 429}, "submit": 1}, {"advance": 48 * HOUR_MS}],
        tmp_path,
    )
    assert steps[0]["disabled"] is True
    assert "обновите страницу позже" in str(steps[0]["status"])
    assert steps[1]["disabled"] is True


def test_double_submit_sends_one_request(tmp_path: Path) -> None:
    steps = run([{"reply": {"status": 200}, "submit": 3}], tmp_path)
    assert steps[0]["calls"] == 1
    assert steps[0]["state"] == "done"
    assert steps[0]["disabled"] is True


@pytest.mark.parametrize(
    ("reply", "state", "enabled"),
    [
        ({"status": 500}, "error", True),
        ({"network": True, "status": 0}, "error", True),
        ({"status": 401}, "refused", False),
        ({"status": 403}, "refused", False),
        ({"status": 404}, "refused", False),
        ({"status": 409}, "refused", False),
    ],
)
def test_each_answer_leaves_the_button_in_the_documented_state(
    reply: dict[str, object], state: str, enabled: bool, tmp_path: Path
) -> None:
    steps = run([{"reply": reply, "submit": 1}], tmp_path)
    assert steps[0]["state"] == state
    assert steps[0]["disabled"] is (not enabled)
