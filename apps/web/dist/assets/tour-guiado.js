(function () {
  "use strict";

  var STEPS = [
    {
      title: "1. Bienvenida a EduAndes AI (ODS 4)",
      role: "General",
      selector: ".ref-topbar",
      body:
        "EduAndes AI convierte material autorizado del docente en actividades evaluativas con cita literal verificada y refuerzos personalizados. En la barra superior puedes alternar entre modo Docente y modo Estudiante, además de activar opciones de Inclusión.",
      actionLabel: "Ir a Panel Docente",
      action: function () {
        switchMode("teacher");
        window.location.hash = "#/";
      },
    },
    {
      title: "2. Crea tu asignatura y sube el material",
      role: "Docente",
      selector: ".ref-rail nav a:nth-child(2)",
      body:
        "En Materiales o Asignaturas creas tu materia, agregas unidades con objetivos de aprendizaje y subes tus apuntes o PDFs autorizados (hasta 40 páginas por archivo).",
      actionLabel: "Abrir Asignaturas",
      action: function () {
        switchMode("teacher");
        window.location.hash = "#/asignaturas";
      },
    },
    {
      title: "3. Genera de 10 a 30 preguntas con IA",
      role: "Docente",
      selector: ".portal-action-card, .tr-generation-card",
      body:
        "En el Panel Docente, haz clic en «Generar práctica» y elige la cantidad de preguntas (10 recomendado para demo, 20 o 30 para evaluación completa). La IA las redacta directamente desde las páginas de tu material.",
      actionLabel: "Ver Panel Docente",
      action: function () {
        switchMode("teacher");
        window.location.hash = "#/";
      },
    },
    {
      title: "4. Revisa la cita literal y publica",
      role: "Docente",
      selector: ".portal-pending, .portal-tiles",
      body:
        "Nada se publica sin tu visto bueno. Entra a «Revisar preguntas», verifica la cita exacta en el documento original, edita si es necesario, aprueba cada pregunta y publica la práctica.",
      actionLabel: "Ver Actividades",
      action: function () {
        switchMode("teacher");
        var navLinks = document.querySelectorAll(".ref-rail nav a");
        if (navLinks[2]) navLinks[2].click();
      },
    },
    {
      title: "5. Responde como Estudiante",
      role: "Estudiante",
      selector: ".ref-mode-switch",
      body:
        "Cambia al modo Estudiante en el interruptor superior, crea o elige un perfil y completa la práctica publicada para registrar tus respuestas.",
      actionLabel: "Cambiar a Estudiante",
      action: function () {
        switchMode("student");
        window.location.hash = "#/";
      },
    },
    {
      title: "6. Refuerzo ampliado (10+ preguntas) e Intervención",
      role: "Estudiante y Docente",
      selector: ".portal-card, .ea-drill-card",
      body:
        "Cuando el estudiante comete un error, recibe un refuerzo guiado en 3 pasos y puede generar una práctica ampliada de 10 a 20 preguntas sobre el tema. El docente ve en «Intervención docente» qué conceptos reforzar en clase.",
      actionLabel: "Finalizar guía",
      action: function () {
        closeTour();
      },
    },
  ];

  var A11Y_KEY = "eduandes.a11y.v1";
  var WELCOME_STORAGE_KEY = "eduandes.welcome.entered";
  var currentIndex = 0;
  var overlay = null;
  var a11yOverlay = null;
  var highlightedEl = null;
  var captionBar = null;
  var ttsFloatingBtn = null;
  var speaking = false;

  function loadA11yState() {
    try {
      var raw = JSON.parse(localStorage.getItem(A11Y_KEY) || "null");
      if (raw && typeof raw === "object") return raw;
    } catch (_) {}
    return { contrast: false, large: false, tts: false, captions: false };
  }

  function saveA11yState(state) {
    try {
      localStorage.setItem(A11Y_KEY, JSON.stringify(state));
    } catch (_) {}
  }

  function announceVisual(text) {
    if (!captionBar) return;
    var span = captionBar.querySelector(".ea-caption-text");
    if (span) span.textContent = text;
  }

  function speakText(text) {
    if (!("speechSynthesis" in window)) return;
    try {
      window.speechSynthesis.cancel();
      if (!text) return;
      var utter = new SpeechSynthesisUtterance(text);
      utter.lang = "es-CL";
      utter.rate = 0.98;
      utter.onend = function () {
        speaking = false;
        updateTtsBtn();
      };
      utter.onerror = function () {
        speaking = false;
        updateTtsBtn();
      };
      speaking = true;
      updateTtsBtn();
      window.speechSynthesis.speak(utter);
    } catch (_) {}
  }

  function readCurrentScreen() {
    var main = document.querySelector("#main-content") || document.body;
    var heading = main.querySelector("h1, h2");
    var title = heading ? heading.textContent.trim() : "EduAndes AI";
    var raw = (main.innerText || "").replace(/\s+/g, " ").trim().slice(0, 900);
    announceVisual("Lectura activa: " + title);
    speakText(raw || title);
  }

  function updateTtsBtn() {
    if (!ttsFloatingBtn) return;
    ttsFloatingBtn.innerHTML = speaking
      ? '<span aria-hidden="true">&#9632;</span> Detener lectura'
      : '<span aria-hidden="true">&#128266;</span> Leer pantalla en voz alta';
  }

  function applyA11yState(state) {
    document.body.classList.toggle("ea-a11y-contrast", Boolean(state.contrast));
    document.body.classList.toggle("ea-a11y-large", Boolean(state.large));
    document.body.classList.toggle("ea-a11y-captions", Boolean(state.captions));

    if (state.captions) {
      if (!captionBar) {
        captionBar = document.createElement("div");
        captionBar.className = "ea-caption-bar";
        captionBar.setAttribute("role", "status");
        captionBar.setAttribute("aria-live", "polite");
        captionBar.innerHTML =
          '<span class="ea-caption-tag">Subtítulos / Alertas visuales</span>' +
          '<span class="ea-caption-text">Modo visual activo: todas las notificaciones e indicaciones se muestran en texto claro sin requerir audio.</span>';
        document.body.appendChild(captionBar);
      }
    } else if (captionBar && captionBar.parentNode) {
      captionBar.parentNode.removeChild(captionBar);
      captionBar = null;
    }

    if (state.tts) {
      if (!ttsFloatingBtn) {
        ttsFloatingBtn = document.createElement("button");
        ttsFloatingBtn.type = "button";
        ttsFloatingBtn.className = "ea-tts-fab";
        ttsFloatingBtn.addEventListener("click", function () {
          if (speaking && "speechSynthesis" in window) {
            window.speechSynthesis.cancel();
            speaking = false;
            updateTtsBtn();
          } else {
            readCurrentScreen();
          }
        });
        document.body.appendChild(ttsFloatingBtn);
      }
      updateTtsBtn();
    } else {
      if ("speechSynthesis" in window) window.speechSynthesis.cancel();
      speaking = false;
      if (ttsFloatingBtn && ttsFloatingBtn.parentNode) {
        ttsFloatingBtn.parentNode.removeChild(ttsFloatingBtn);
        ttsFloatingBtn = null;
      }
    }
  }

  function closeA11yModal() {
    if (a11yOverlay && a11yOverlay.parentNode) {
      a11yOverlay.parentNode.removeChild(a11yOverlay);
    }
    a11yOverlay = null;
  }

  function openA11yModal() {
    if (a11yOverlay) return;
    var state = loadA11yState();
    a11yOverlay = document.createElement("div");
    a11yOverlay.className = "ea-tour-backdrop";
    a11yOverlay.setAttribute("role", "dialog");
    a11yOverlay.setAttribute("aria-modal", "true");
    a11yOverlay.setAttribute("aria-label", "Opciones de Inclusión y Accesibilidad");
    a11yOverlay.addEventListener("click", function (e) {
      if (e.target === a11yOverlay) closeA11yModal();
    });
    a11yOverlay.innerHTML =
      '<div class="ea-tour-modal ea-a11y-modal">' +
        '<div class="ea-tour-head">' +
          '<span class="ea-tour-badge">ODS 4 · Inclusión y Accesibilidad</span>' +
          '<button type="button" class="ea-tour-close" aria-label="Cerrar inclusión">&times;</button>' +
        "</div>" +
        '<h2 class="ea-tour-title" style="margin-top:12px">Apoyo para discapacidad visual y auditiva</h2>' +
        '<p class="ea-tour-body">Activa las adaptaciones que necesites. Se guardan automáticamente en este navegador.</p>' +
        '<div class="ea-a11y-grid">' +
          '<label class="ea-a11y-option">' +
            '<input type="checkbox" data-a11y="tts"' + (state.tts ? " checked" : "") + "/>" +
            '<div><strong>Lectura en voz alta (Discapacidad visual)</strong><small>Activa un lector de voz en español para escuchar preguntas, opciones y explicaciones.</small></div>' +
          "</label>" +
          '<label class="ea-a11y-option">' +
            '<input type="checkbox" data-a11y="contrast"' + (state.contrast ? " checked" : "") + "/>" +
            '<div><strong>Alto contraste (Discapacidad visual)</strong><small>Refuerza textos oscuros, fondos puros y bordes nítidos para máxima legibilidad.</small></div>' +
          "</label>" +
          '<label class="ea-a11y-option">' +
            '<input type="checkbox" data-a11y="large"' + (state.large ? " checked" : "") + "/>" +
            '<div><strong>Texto ampliado A+ (Discapacidad visual)</strong><small>Aumenta el tamaño de letra e interlineado en botones, tarjetas y alternativas.</small></div>' +
          "</label>" +
          '<label class="ea-a11y-option">' +
            '<input type="checkbox" data-a11y="captions"' + (state.captions ? " checked" : "") + "/>" +
            '<div><strong>Subtítulos y alertas visuales (Discapacidad auditiva)</strong><small>Muestra una barra visual permanente con avisos claros de cada acción sin depender de sonido.</small></div>' +
          "</label>" +
        "</div>" +
        '<div class="ea-tour-actions" style="margin-top:16px;justify-content:flex-end">' +
          '<button type="button" class="ea-tour-btn ea-tour-btn-primary ea-a11y-done">Listo</button>' +
        "</div>" +
      "</div>";

    a11yOverlay.querySelector(".ea-tour-close").addEventListener("click", closeA11yModal);
    a11yOverlay.querySelector(".ea-a11y-done").addEventListener("click", closeA11yModal);
    var inputs = a11yOverlay.querySelectorAll("input[data-a11y]");
    for (var i = 0; i < inputs.length; i++) {
      inputs[i].addEventListener("change", function (e) {
        var key = e.currentTarget.getAttribute("data-a11y");
        state[key] = e.currentTarget.checked;
        saveA11yState(state);
        applyA11yState(state);
        announceVisual("Preferencia de inclusión actualizada: " + key);
      });
    }
    document.body.appendChild(a11yOverlay);
  }

  function switchMode(target) {
    var btns = document.querySelectorAll(".ref-mode-switch button");
    if (!btns || btns.length < 2) return;
    var wantedText = target === "teacher" ? "docente" : "estudiante";
    for (var i = 0; i < btns.length; i++) {
      var label = (btns[i].textContent || "").toLowerCase();
      if (label.indexOf(wantedText) !== -1 && btns[i].getAttribute("aria-pressed") !== "true") {
        btns[i].click();
        break;
      }
    }
  }

  function clearHighlight() {
    if (highlightedEl) {
      highlightedEl.classList.remove("ea-tour-highlight");
      highlightedEl = null;
    }
  }

  function applyHighlight(selector) {
    clearHighlight();
    if (!selector) return;
    var el = document.querySelector(selector);
    if (el) {
      el.classList.add("ea-tour-highlight");
      highlightedEl = el;
      try {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      } catch (_) {}
    }
  }

  function closeTour() {
    clearHighlight();
    if (overlay && overlay.parentNode) {
      overlay.parentNode.removeChild(overlay);
    }
    overlay = null;
    document.removeEventListener("keydown", onKeyDown);
  }

  function onKeyDown(e) {
    if (e.key === "Escape") {
      closeTour();
      closeA11yModal();
    } else if (overlay && e.key === "ArrowRight" && currentIndex < STEPS.length - 1) {
      renderStep(currentIndex + 1);
    } else if (overlay && e.key === "ArrowLeft" && currentIndex > 0) {
      renderStep(currentIndex - 1);
    }
  }

  function renderStep(idx) {
    currentIndex = idx;
    var step = STEPS[idx];
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "ea-tour-backdrop";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      overlay.setAttribute("aria-label", "Guía interactiva de EduAndes AI");
      overlay.addEventListener("click", function (e) {
        if (e.target === overlay) closeTour();
      });
      document.body.appendChild(overlay);
      document.addEventListener("keydown", onKeyDown);
    }

    var pct = Math.round(((idx + 1) / STEPS.length) * 100);
    overlay.innerHTML =
      '<div class="ea-tour-modal">' +
        '<div class="ea-tour-head">' +
          '<span class="ea-tour-badge">' + step.role + " · Paso " + (idx + 1) + " de " + STEPS.length + "</span>" +
          '<button type="button" class="ea-tour-close" aria-label="Cerrar guía">&times;</button>' +
        "</div>" +
        '<div class="ea-tour-progress"><span style="width:' + pct + '%"></span></div>' +
        '<h2 class="ea-tour-title">' + step.title + "</h2>" +
        '<p class="ea-tour-body">' + step.body + "</p>" +
        '<div class="ea-tour-dots">' +
          STEPS.map(function (_, i) {
            return '<button type="button" class="ea-tour-dot' + (i === idx ? " is-active" : "") + '" data-step="' + i + '" aria-label="Ir al paso ' + (i + 1) + '"></button>';
          }).join("") +
        "</div>" +
        '<div class="ea-tour-actions">' +
          '<button type="button" class="ea-tour-btn ea-tour-btn-ghost" data-nav="prev"' + (idx === 0 ? " disabled" : "") + ">Anterior</button>" +
          '<button type="button" class="ea-tour-btn ea-tour-btn-action" data-nav="action">' + step.actionLabel + "</button>" +
          '<button type="button" class="ea-tour-btn ea-tour-btn-primary" data-nav="next">' + (idx === STEPS.length - 1 ? "Listo" : "Siguiente") + "</button>" +
        "</div>" +
      "</div>";

    overlay.querySelector(".ea-tour-close").addEventListener("click", closeTour);
    overlay.querySelector('[data-nav="prev"]').addEventListener("click", function () {
      if (currentIndex > 0) renderStep(currentIndex - 1);
    });
    overlay.querySelector('[data-nav="next"]').addEventListener("click", function () {
      if (currentIndex < STEPS.length - 1) renderStep(currentIndex + 1);
      else closeTour();
    });
    overlay.querySelector('[data-nav="action"]').addEventListener("click", function () {
      step.action();
      setTimeout(function () {
        applyHighlight(step.selector);
      }, 150);
    });
    var dots = overlay.querySelectorAll(".ea-tour-dot");
    for (var d = 0; d < dots.length; d++) {
      dots[d].addEventListener("click", function (e) {
        renderStep(Number(e.currentTarget.getAttribute("data-step")));
      });
    }

    announceVisual(step.title + ": " + step.body);
    applyHighlight(step.selector);
  }

  function chileFlagSvg() {
    return (
      '<svg class="ea-flag" viewBox="0 0 36 24" width="27" height="18" aria-label="Bandera de Chile" role="img">' +
        '<rect width="36" height="12" fill="#ffffff"/>' +
        '<rect y="12" width="36" height="12" fill="#d52b1e"/>' +
        '<rect width="12" height="12" fill="#0039a6"/>' +
        '<polygon points="6,2.6 6.85,5.15 9.5,5.15 7.35,6.75 8.15,9.3 6,7.75 3.85,9.3 4.65,6.75 2.5,5.15 5.15,5.15" fill="#ffffff"/>' +
      "</svg>"
    );
  }

  function ecuadorFlagSvg() {
    return (
      '<svg class="ea-flag" viewBox="0 0 36 24" width="27" height="18" aria-label="Bandera de Ecuador" role="img">' +
        '<rect width="36" height="12" fill="#ffd100"/>' +
        '<rect y="12" width="36" height="6" fill="#034ea2"/>' +
        '<rect y="18" width="36" height="6" fill="#ed1c24"/>' +
        '<path d="M13.2 8.2 Q18 6.4 22.8 8.2" fill="none" stroke="#5c4314" stroke-width="1.1" stroke-linecap="round"/>' +
        '<ellipse cx="18" cy="12" rx="3.1" ry="3.8" fill="#8cc6e8" stroke="#c79b2c" stroke-width="0.9"/>' +
        '<path d="M15.4 13.6 Q18 11.4 20.6 13.6 L20.4 14.8 Q18 15.6 15.6 14.8 Z" fill="#3a6b35"/>' +
        '<circle cx="18" cy="10.2" r="1.05" fill="#ffd100"/>' +
      "</svg>"
    );
  }

  function binationalCrestSvg() {
    return (
      '<svg class="ea-crest" viewBox="0 0 28 28" width="22" height="22" aria-hidden="true">' +
        '<path d="M14 2.5 L24 6.5 V14.5 C24 20.5 19.6 24.4 14 26 C8.4 24.4 4 20.5 4 14.5 V6.5 Z" fill="#0b285e" stroke="#c9a227" stroke-width="1.4"/>' +
        '<path d="M14 3.5 L23 7 V14.5 C23 19.6 19.2 23.1 14 24.7 Z" fill="#8f1d28" opacity="0.88"/>' +
        '<circle cx="14" cy="11.2" r="3.4" fill="none" stroke="#ffd100" stroke-width="1.2"/>' +
        '<polygon points="14,13.6 14.9,16.1 17.5,16.1 15.4,17.6 16.2,20.1 14,18.6 11.8,20.1 12.6,17.6 10.5,16.1 13.1,16.1" fill="#ffffff"/>' +
      "</svg>"
    );
  }

  function topbarBackgroundEl() {
    var wrap = document.createElement("div");
    wrap.className = "ea-topbar-bg";
    wrap.setAttribute("aria-hidden", "true");
    wrap.innerHTML =
      '<div class="ea-topbar-mural"></div>' +
      '<div class="ea-topbar-scrim"></div>';
    return wrap;
  }

  function mountReinforcementDrill() {
    var page = document.querySelector(".sr-reinforcement-page");
    if (!page) return;
    var hash = window.location.hash;
    var match = hash.match(/^#\/refuerzo\/([^/?]+)(?:\/([^/?]+))?/);
    if (!match) return;
    var attemptId = match[1];
    var reinforcementId = match[2] || "";
    if (!reinforcementId) {
      if (page.getAttribute("data-ea-resolving") === attemptId) return;
      page.setAttribute("data-ea-resolving", attemptId);
      var stId = "";
      try { stId = localStorage.getItem("educore.student-id") || ""; } catch (_) {}
      fetch("/api/quiz-attempts/" + encodeURIComponent(attemptId) + (stId ? "?student_id=" + encodeURIComponent(stId) : ""))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (data && data.reinforcement_id) {
            renderDrillCard(page, data.reinforcement_id);
          }
        })
        .catch(function () {});
      return;
    }
    renderDrillCard(page, reinforcementId);
  }

  function renderDrillCard(page, reinforcementId) {
    if (page.querySelector('.ea-drill-card[data-reinf="' + reinforcementId + '"]')) return;

    var card = document.createElement("section");
    card.className = "sr-card ea-drill-card";
    card.setAttribute("data-reinf", reinforcementId);
    card.innerHTML =
      '<div class="sr-card-title">' +
        '<span class="sr-icon-tile sr-lilac"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/></svg></span>' +
        '<div>' +
          '<p class="sr-eyebrow">PRÁCTICA AMPLIADA DE REFUERZO</p>' +
          '<h2>Ejercita con 10 o más preguntas del tema</h2>' +
        "</div>" +
      "</div>" +
      '<p class="sr-subtitle">Genera una serie completa de 10, 15 o 20 preguntas verificadas sobre el material de la unidad para reforzar tu dominio.</p>' +
      '<div class="ea-drill-controls">' +
        '<label class="ea-drill-select-wrap">Cantidad: ' +
          '<select class="ea-drill-count">' +
            '<option value="10" selected>10 preguntas (Recomendado)</option>' +
            '<option value="15">15 preguntas</option>' +
            '<option value="20">20 preguntas</option>' +
          "</select>" +
        "</label>" +
        '<label class="ea-drill-select-wrap">Formato: ' +
          '<select class="ea-drill-type">' +
            '<option value="mixed" selected>Mixta (Selección y Desarrollo)</option>' +
            '<option value="multiple_choice">Solo selección múltiple</option>' +
            '<option value="open">Solo desarrollo (abierta)</option>' +
          "</select>" +
        "</label>" +
        '<button type="button" class="sr-button sr-primary ea-drill-start">Generar práctica de refuerzo</button>' +
      "</div>" +
      '<div class="ea-drill-stage"></div>';

    var stage = card.querySelector(".ea-drill-stage");
    var startBtn = card.querySelector(".ea-drill-start");
    var countSel = card.querySelector(".ea-drill-count");
    var typeSel = card.querySelector(".ea-drill-type");

    function renderDrillPlayer(drill) {
      var items = drill.items || [];
      var idx = 0;
      var answers = {};
      var openText = {};
      var checked = {};

      function draw() {
        var item = items[idx];
        var isChoice = item.type === "multiple_choice";
        var chosen = answers[item.id];
        var typed = openText[item.id] || "";
        var isChecked = Boolean(checked[item.id]);
        var letters = ["A", "B", "C", "D"];
        var score = items.filter(function (it) { return it.type === "multiple_choice" && checked[it.id] && answers[it.id] === it.correct_index; }).length;
        var gradedTotal = items.filter(function (it) { return it.type === "multiple_choice" && checked[it.id]; }).length;
        var canCheck = isChoice ? chosen !== undefined : typed.trim().length > 0;

        stage.innerHTML =
          '<div class="ea-drill-progress">' +
            '<span>Pregunta <strong>' + (idx + 1) + '</strong> de <strong>' + items.length + '</strong> · <em>' + (isChoice ? "Selección múltiple" : "Desarrollo (abierta)") + "</em></span>" +
            '<span>Aciertos selección: <strong>' + score + " / " + gradedTotal + "</strong></span>" +
          "</div>" +
          '<h3 class="sr-stem">' + item.question + "</h3>" +
          (isChoice
            ? '<div class="sr-options">' +
                item.options.map(function (opt, oIdx) {
                  var cls = "sr-option";
                  if (chosen === oIdx) cls += " sr-selected";
                  if (isChecked && oIdx === item.correct_index) cls += " sr-correct";
                  else if (isChecked && chosen === oIdx && oIdx !== item.correct_index) cls += " sr-incorrect";
                  return (
                    '<label class="' + cls + '">' +
                      '<input type="radio" name="ea_drill_opt" value="' + oIdx + '"' + (chosen === oIdx ? " checked" : "") + (isChecked ? " disabled" : "") + "/>" +
                      '<span class="sr-option-letter">' + letters[oIdx] + ".</span>" +
                      "<span>" + opt + "</span>" +
                    "</label>"
                  );
                }).join("") +
              "</div>"
            : '<div class="ea-drill-open-wrap">' +
                '<textarea class="textarea ea-drill-open-input" rows="3" placeholder="Escribe tu respuesta con tus palabras..."' + (isChecked ? " disabled" : "") + ">" + typed.replace(/</g, "&lt;") + "</textarea>" +
              "</div>") +
          (isChecked
            ? '<div class="ea-drill-feedback ' + (isChoice ? (chosen === item.correct_index ? "is-ok" : "is-warn") : "is-ok") + '">' +
                "<strong>" + (isChoice ? (chosen === item.correct_index ? "¡Correcto!" : "Revisa la idea clave · Respuesta: " + item.answer) : "Respuesta de referencia · " + item.answer) + "</strong>" +
                "<p>" + item.explanation + "</p>" +
                "<small>Cita literal (" + item.citation.label + "): «" + item.citation.quote + "»</small>" +
              "</div>"
            : "") +
          '<div class="ea-drill-nav">' +
            '<button type="button" class="sr-button sr-secondary" data-d="prev"' + (idx === 0 ? " disabled" : "") + ">Anterior</button>" +
            (!isChecked
              ? '<button type="button" class="sr-button sr-primary" data-d="check"' + (!canCheck ? " disabled" : "") + ">Comprobar respuesta</button>"
              : '<button type="button" class="sr-button sr-primary" data-d="next"' + (idx === items.length - 1 ? " disabled" : "") + ">Siguiente pregunta</button>") +
          "</div>";

        var radios = stage.querySelectorAll('input[name="ea_drill_opt"]');
        for (var r = 0; r < radios.length; r++) {
          radios[r].addEventListener("change", function (e) {
            answers[item.id] = Number(e.currentTarget.value);
            draw();
          });
        }
        var openArea = stage.querySelector(".ea-drill-open-input");
        if (openArea) {
          openArea.addEventListener("input", function (e) {
            openText[item.id] = e.currentTarget.value;
            var chk = stage.querySelector('[data-d="check"]');
            if (chk) chk.disabled = e.currentTarget.value.trim().length === 0;
          });
        }
        var prevBtn = stage.querySelector('[data-d="prev"]');
        if (prevBtn) prevBtn.addEventListener("click", function () { if (idx > 0) { idx--; draw(); } });
        var chkBtn = stage.querySelector('[data-d="check"]');
        if (chkBtn) chkBtn.addEventListener("click", function () {
          checked[item.id] = true;
          var ok = isChoice ? answers[item.id] === item.correct_index : true;
          announceVisual(isChoice ? (ok ? "Respuesta correcta en pregunta " + (idx + 1) : "Respuesta incorrecta. Clave: " + item.answer) : "Referencia de desarrollo: " + item.answer);
          if (loadA11yState().tts) speakText((isChoice ? (ok ? "Correcto. " : "Revisa la idea clave. ") : "Referencia: " + item.answer + ". ") + item.explanation);
          draw();
        });
        var nxtBtn = stage.querySelector('[data-d="next"]');
        if (nxtBtn) nxtBtn.addEventListener("click", function () {
          if (idx < items.length - 1) { idx++; draw(); }
        });
      }

      draw();
    }

    startBtn.addEventListener("click", function () {
      var wanted = Number(countSel.value) || 10;
      var qType = (typeSel && typeSel.value) || "mixed";
      startBtn.disabled = true;
      startBtn.textContent = "Generando " + wanted + " preguntas…";
      stage.innerHTML = '<div class="sr-loading"><span class="spinner"></span> Preparando ' + wanted + ' preguntas verificadas con IA desde el material…</div>';
      announceVisual("Generando práctica de refuerzo de " + wanted + " preguntas.");

      var studentId = "";
      try { studentId = localStorage.getItem("educore.student-id") || ""; } catch (_) {}
      var url = "/api/reinforcements/" + encodeURIComponent(reinforcementId) + "/practice-drill" + (studentId ? "?student_id=" + encodeURIComponent(studentId) : "");

      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: wanted, question_type: qType }),
      })
        .then(function (resp) {
          if (!resp.ok) return resp.json().then(function (err) { throw new Error(err.detail || "No se pudo generar la práctica."); });
          return resp.json();
        })
        .then(function (drill) {
          startBtn.disabled = false;
          startBtn.textContent = "Serie lista (" + (drill.items || []).length + " preguntas)";
          announceVisual("Práctica ampliada lista con " + (drill.items || []).length + " preguntas.");
          renderDrillPlayer(drill);
        })
        .catch(function (err) {
          startBtn.disabled = false;
          startBtn.textContent = "Reintentar práctica de refuerzo";
          stage.innerHTML = '<p class="sr-alert sr-error">' + (err.message || "Error generando la práctica.") + "</p>";
        });
    });

    page.appendChild(card);
  }

  function logoutToWelcome() {
    closeTour();
    closeA11yModal();
    if ("speechSynthesis" in window) {
      try { window.speechSynthesis.cancel(); } catch (_) {}
    }
    try {
      sessionStorage.removeItem(WELCOME_STORAGE_KEY);
      localStorage.removeItem("educore.student-id");
    } catch (_) {}
    window.location.hash = "#/";
    showWelcomeScreen();
    announceVisual("Sesión cerrada. Regresando a la portada de EduAndes AI.");
  }

  function ensureBannerButton() {
    var topbar = document.querySelector(".ref-topbar");
    if (!topbar) return;

    if (!topbar.querySelector(".ea-topbar-bg")) {
      topbar.insertBefore(topbarBackgroundEl(), topbar.firstChild);
    }

    var switchEl = topbar.querySelector(".ref-mode-switch");

    if (!topbar.querySelector(".ea-binational-pill")) {
      var pill = document.createElement("div");
      pill.className = "ea-binational-pill";
      pill.innerHTML =
        chileFlagSvg() +
        binationalCrestSvg() +
        '<span class="ea-binational-text"><strong>Chile – Ecuador</strong><span class="ea-binational-sub"> · Educación Binacional</span></span>' +
        ecuadorFlagSvg();
      if (switchEl && switchEl.parentNode === topbar) {
        topbar.insertBefore(pill, switchEl);
      } else {
        topbar.appendChild(pill);
      }
    }

    if (!topbar.querySelector(".ea-inclusion-trigger")) {
      var incBtn = document.createElement("button");
      incBtn.type = "button";
      incBtn.className = "ea-inclusion-trigger";
      incBtn.innerHTML =
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<circle cx="12" cy="4.5" r="2"/><path d="M5 9h14M12 9v6m-4 6 4-6 4 6"/></svg>' +
        "<span>Inclusión</span>";
      incBtn.addEventListener("click", openA11yModal);
      if (switchEl && switchEl.parentNode === topbar) {
        topbar.insertBefore(incBtn, switchEl);
      } else {
        topbar.appendChild(incBtn);
      }
    }

    if (!topbar.querySelector(".ea-guide-trigger")) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ea-guide-trigger";
      btn.innerHTML =
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
        "<span>Cómo usar</span>";
      btn.addEventListener("click", function () {
        renderStep(0);
      });

      if (switchEl && switchEl.parentNode === topbar) {
        topbar.insertBefore(btn, switchEl);
      } else {
        topbar.appendChild(btn);
      }
    }

    if (!topbar.querySelector(".ea-logout-trigger")) {
      var logoutBtn = document.createElement("button");
      logoutBtn.type = "button";
      logoutBtn.className = "ea-logout-trigger";
      logoutBtn.setAttribute("aria-label", "Cerrar sesión y volver al inicio");
      logoutBtn.innerHTML =
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>' +
        "<span>Cerrar sesión</span>";
      logoutBtn.addEventListener("click", logoutToWelcome);
      topbar.appendChild(logoutBtn);
    }

    mountReinforcementDrill();
  }

  function hoistedChileFlagHtml() {
    return (
      '<div class="ea-hoist" aria-label="Bandera de Chile en asta" role="img">' +
        '<div class="ea-pole"><span class="ea-finial"></span></div>' +
        '<div class="ea-cloth ea-cloth-cl">' +
          '<svg viewBox="0 0 360 240" preserveAspectRatio="none">' +
            '<defs>' +
              '<clipPath id="eaWaveClipCL">' +
                '<path d="M0,0 Q90,8 180,0 T360,4 L360,236 Q270,228 180,236 T0,240 Z">' +
                  '<animate attributeName="d" dur="3.6s" repeatCount="indefinite" values="' +
                    'M0,0 Q90,8 180,0 T360,4 L360,236 Q270,228 180,236 T0,240 Z;' +
                    'M0,0 Q90,-7 180,4 T360,-2 L360,240 Q270,233 180,240 T0,236 Z;' +
                    'M0,0 Q90,8 180,0 T360,4 L360,236 Q270,228 180,236 T0,240 Z"/>' +
                '</path>' +
              '</clipPath>' +
            '</defs>' +
            '<g clip-path="url(#eaWaveClipCL)">' +
              '<rect width="360" height="120" fill="#ffffff"/>' +
              '<rect y="120" width="360" height="120" fill="#d52b1e"/>' +
              '<rect width="120" height="120" fill="#0039a6"/>' +
              '<polygon points="60,24 68.8,51 97,51 74.2,67.6 82.8,94.6 60,78 37.2,94.6 45.8,67.6 23,51 51.2,51" fill="#ffffff"/>' +
            '</g>' +
          "</svg>" +
          '<span class="ea-cloth-sheen"></span>' +
        "</div>" +
      "</div>"
    );
  }

  function hoistedEcuadorFlagHtml() {
    return (
      '<div class="ea-hoist" aria-label="Bandera de Ecuador en asta" role="img">' +
        '<div class="ea-pole"><span class="ea-finial"></span></div>' +
        '<div class="ea-cloth ea-cloth-ec">' +
          '<svg viewBox="0 0 360 240" preserveAspectRatio="none">' +
            '<defs>' +
              '<clipPath id="eaWaveClipEC">' +
                '<path d="M0,0 Q90,7 180,0 T360,4 L360,236 Q270,229 180,236 T0,240 Z">' +
                  '<animate attributeName="d" dur="3.9s" repeatCount="indefinite" values="' +
                    'M0,0 Q90,7 180,0 T360,4 L360,236 Q270,229 180,236 T0,240 Z;' +
                    'M0,0 Q90,-6 180,5 T360,-2 L360,240 Q270,234 180,240 T0,236 Z;' +
                    'M0,0 Q90,7 180,0 T360,4 L360,236 Q270,229 180,236 T0,240 Z"/>' +
                '</path>' +
              '</clipPath>' +
            '</defs>' +
            '<g clip-path="url(#eaWaveClipEC)">' +
              '<rect width="360" height="120" fill="#ffd100"/>' +
              '<rect y="120" width="360" height="60" fill="#034ea2"/>' +
              '<rect y="180" width="360" height="60" fill="#ed1c24"/>' +
              /* Escudo estilizado en el centro de la bandera ecuatoriana */
              '<path d="M132 82 Q180 60 228 82" fill="none" stroke="#4d3610" stroke-width="8" stroke-linecap="round"/>' +
              '<ellipse cx="180" cy="120" rx="30" ry="37" fill="#8cc6e8" stroke="#c79b2c" stroke-width="6"/>' +
              '<path d="M155 135 Q180 113 205 135 L203 148 Q180 155 157 148 Z" fill="#356e30"/>' +
              '<circle cx="180" cy="102" r="9.5" fill="#ffd100"/>' +
            '</g>' +
          "</svg>" +
          '<span class="ea-cloth-sheen"></span>' +
        "</div>" +
      "</div>"
    );
  }

  function showWelcomeScreen() {
    if (document.querySelector(".ea-welcome-overlay")) return;
    try {
      if (sessionStorage.getItem(WELCOME_STORAGE_KEY) === "1") return;
    } catch (_) {}

    var screen = document.createElement("div");
    screen.className = "ea-welcome-overlay";
    screen.setAttribute("role", "dialog");
    screen.setAttribute("aria-modal", "true");
    screen.setAttribute("aria-label", "Bienvenida a EduAndes AI");
    screen.innerHTML =
      '<div class="ea-welcome-stage">' +
        '<div class="ea-welcome-flag-slot ea-slot-cl">' +
          hoistedChileFlagHtml() +
        "</div>" +
        '<main class="ea-welcome-center">' +
          '<div class="ea-welcome-card">' +
            '<div class="ea-welcome-badges">' +
              binationalCrestSvg() +
              "<span>Encuentro Binacional Chile – Ecuador</span>" +
            "</div>" +
            '<button type="button" class="ea-welcome-brand-btn" aria-label="Entrar a EduAndes AI">' +
              '<span class="ea-welcome-title">EduAndes <b>AI</b></span>' +
              '<span class="ea-welcome-enter-pill">Entrar <span aria-hidden="true">&#8594;</span></span>' +
            "</button>" +
            '<p class="ea-welcome-subtitle">ODS 4 · Educación de Calidad e Inclusiva con IA Verificada</p>' +
          "</div>" +
        "</main>" +
        '<div class="ea-welcome-flag-slot ea-slot-ec">' +
          hoistedEcuadorFlagHtml() +
        "</div>" +
      "</div>" +
      '<footer class="ea-welcome-footer">' +
        '<span class="ea-welcome-ribbon" aria-hidden="true"></span>' +
        '<span class="ea-welcome-hack">Hackathon Chile – Ecuador 2026</span>' +
      "</footer>";

    function enterSystem() {
      try {
        sessionStorage.setItem(WELCOME_STORAGE_KEY, "1");
      } catch (_) {}
      document.documentElement.classList.remove("ea-welcome-open");
      document.body.classList.remove("ea-welcome-open");
      screen.classList.add("is-leaving");
      setTimeout(function () {
        if (screen.parentNode) screen.parentNode.removeChild(screen);
      }, 280);
    }

    screen.querySelector(".ea-welcome-brand-btn").addEventListener("click", enterSystem);
    document.documentElement.classList.add("ea-welcome-open");
    document.body.classList.add("ea-welcome-open");
    document.body.appendChild(screen);
  }

  var observer = new MutationObserver(ensureBannerButton);
  function init() {
    applyA11yState(loadA11yState());
    showWelcomeScreen();
    ensureBannerButton();
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("hashchange", function () {
      setTimeout(mountReinforcementDrill, 120);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
