// Повтор отказа без перезагрузки страницы. Без JS форма работает как раньше:
// обычный POST и редирект 303. Скрипт только перехватывает отправку.
(function () {
  "use strict";

  // Лимит бывает до суток (DAILY_LIMIT). Один длинный setTimeout ненадёжен: во
  // фоновой или уснувшей вкладке таймеры замирают, поэтому сверяем часы раз в минуту.
  var CHECK_EVERY_MS = 60000;

  function clock(ms) {
    var at = new Date(ms);
    var pad = function (n) {
      return (n < 10 ? "0" : "") + n;
    };
    return pad(at.getHours()) + ":" + pad(at.getMinutes());
  }

  function parseBody(response) {
    return response.text().then(function (text) {
      try {
        return JSON.parse(text);
      } catch (e) {
        return {};
      }
    });
  }

  function init(form) {
    var button = form.querySelector("button");
    var row = form.closest("tr");
    var status = form.parentElement.querySelector("[role='status']");
    var inFlight = false;

    function show(message, kind) {
      if (!status) return;
      status.textContent = message;
      status.className = "retry-status " + kind;
    }

    function finish(state, enable) {
      inFlight = false;
      form.removeAttribute("aria-busy");
      if (row) row.setAttribute("data-retry-state", state);
      if (button) button.disabled = !enable;
    }

    function unlockAt(untilMs) {
      if (Date.now() >= untilMs) {
        if (row) row.setAttribute("data-retry-state", "ready");
        if (button && !inFlight) button.disabled = false;
        show("Лимит прошёл — можно повторить.", "mute");
        return;
      }
      setTimeout(function () {
        unlockAt(untilMs);
      }, Math.min(untilMs - Date.now(), CHECK_EVERY_MS));
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (inFlight) return;
      inFlight = true;
      form.setAttribute("aria-busy", "true");
      if (button) button.disabled = true;
      show("Отправляю…", "mute");

      fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      })
        .then(function (response) {
          return parseBody(response).then(function (body) {
            var message = body.message || "Ответ сервера: " + response.status;
            if (response.ok) {
              show(message, "good");
              finish("done", false);
            } else if (response.status === 429) {
              // Отказ не сохраняет ключ формы, поэтому та же форма после лимита безопасна.
              var wait = parseInt(response.headers.get("Retry-After") || "", 10);
              finish("limited", false);
              if (wait > 0) {
                var until = Date.now() + wait * 1000;
                show(message + " Кнопка включится сама около " + clock(until) +
                  " (по часам этого компьютера).", "bad");
                unlockAt(until);
              } else {
                show(message + " Срок не сообщён — обновите страницу позже.", "bad");
              }
            } else if (response.status >= 500) {
              // Исход неизвестен; тот же request_id не заведёт вторую попытку.
              show(message + " Можно нажать ещё раз.", "bad");
              finish("error", true);
            } else {
              // 401/403/404/409: правило сказало «нельзя» — страницу надо открыть заново.
              show(message, "bad");
              finish("refused", false);
            }
          });
        })
        .catch(function () {
          show("Нет связи с сервером. Можно нажать ещё раз: повтор той же формы безопасен.", "bad");
          finish("error", true);
        });
    });
  }

  var forms = document.querySelectorAll("form[action^='/rejects/'][action$='/retry']");
  Array.prototype.forEach.call(forms, init);
})();
