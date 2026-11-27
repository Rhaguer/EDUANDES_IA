(function () {
  "use strict";

  var COUNSELOR_KEY = "eduandes.auth.is_counselor";
  var COUNSELOR_VIEW_KEY = "eduandes.orientacion.view";
  var AUTH_EMAIL_KEY = "eduandes.auth.email";

  var SECTIONS = [
    { id: "overview", label: "1. Detección Temprana de Rezago" },
    { id: "students", label: "2. Trazabilidad por Estudiante" },
    { id: "alerts", label: "3. Alertas de Bienestar" },
    { id: "interventions", label: "4. Bitácora e Intervenciones" },
    { id: "courses", label: "5. Cobertura y Privacidad RBAC" },
  ];

  var state = {
    section: "overview",
    data: null,
    studentFilter: "",
    riskFilter: "all",
    toast: "",
    loading: false,
  };

  function esc(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function isCounselorLoggedIn() {
    try {
      return (
        sessionStorage.getItem("eduandes.welcome.entered") === "1" &&
        sessionStorage.getItem(COUNSELOR_KEY) === "1"
      );
    } catch (_) {
      return false;
    }
  }

  function api(url, opts) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, opts || {})).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data.detail || "Error de servidor (" + r.status + ")");
        return data;
      });
    });
  }

  function showToast(msg) {
    state.toast = msg;
    render();
    setTimeout(function () {
      if (state.toast === msg) {
        state.toast = "";
        render();
      }
    }, 3200);
  }

  function loadState() {
    state.loading = true;
    render();
    return api("/api/orientacion/state")
      .then(function (d) {
        state.data = d;
        state.loading = false;
        render();
      })
      .catch(function (err) {
        state.loading = false;
        showToast("No se pudo cargar Orientación: " + err.message);
      });
  }

  function riskBadge(risk) {
    if (risk === "Alto") return "<span class='ea-adm-badge is-red'>Riesgo Alto</span>";
    if (risk === "Medio") return "<span class='ea-adm-badge is-amber'>Riesgo Medio</span>";
    if (risk === "Bajo") return "<span class='ea-adm-badge is-green'>Adecuado</span>";
    return "<span class='ea-adm-badge is-blue'>Sin actividad aún</span>";
  }

  function alertStatusBadge(status) {
    var s = String(status || "").toLowerCase();
    if (s.indexOf("resuelta") !== -1) return "<span class='ea-adm-badge is-green'>" + esc(status) + "</span>";
    if (s.indexOf("entrevista") !== -1) return "<span class='ea-adm-badge is-blue'>" + esc(status) + "</span>";
    if (s.indexOf("seguimiento") !== -1) return "<span class='ea-adm-badge is-amber'>" + esc(status) + "</span>";
    return "<span class='ea-adm-badge is-red'>" + esc(status) + "</span>";
  }

  function renderOverview(d) {
    var k = d.kpis || {};
    var alerts = d.early_alerts || [];
    var students = d.students || [];
    var priorityStudents = students.filter(function (s) {
      return s.risk_level === "Alto" || s.risk_level === "Medio" || s.attempts_count === 0;
    });

    return (
      "<div class='ea-adm-header'><div><h2>Resumen Ejecutivo · Detección Temprana de Rezago</h2>" +
      "<p>Visión consolidada de acompañamiento escolar, alertas de bienestar y privacidad segregada por rol.</p></div>" +
      "<button class='ea-adm-btn is-primary' id='ea-ori-refresh'>Actualizar datos</button></div>" +

      "<div class='ea-adm-grid'>" +
        "<div class='ea-adm-kpi'><div class='ea-adm-kpi-label'>Estudiantes monitoreados</div>" +
          "<div class='ea-adm-kpi-value'>" + esc(k.students_total) + "</div>" +
          "<div class='ea-adm-kpi-sub'>Perfiles activos en red Chile–Ecuador</div></div>" +
        "<div class='ea-adm-kpi'><div class='ea-adm-kpi-label'>Rezago / Riesgo detectado</div>" +
          "<div class='ea-adm-kpi-value' style='color:" + (k.at_risk_count > 0 ? "#b82318" : "#0f6e40") + "'>" + esc(k.at_risk_count) + "</div>" +
          "<div class='ea-adm-kpi-sub'>Bajo 78% de acierto acumulado</div></div>" +
        "<div class='ea-adm-kpi'><div class='ea-adm-kpi-label'>Alertas de bienestar activas</div>" +
          "<div class='ea-adm-kpi-value' style='color:#9e6b00'>" + esc(k.active_alerts_count) + "</div>" +
          "<div class='ea-adm-kpi-sub'>Derivaciones tempranas abiertas</div></div>" +
        "<div class='ea-adm-kpi'><div class='ea-adm-kpi-label'>Dominio académico promedio</div>" +
          "<div class='ea-adm-kpi-value'>" + (k.avg_mastery_pct != null ? esc(k.avg_mastery_pct) + "%" : "Sin datos") + "</div>" +
          "<div class='ea-adm-kpi-sub'>" + esc(k.attempts_total) + " prácticas · " + esc(k.reinforcements_total) + " refuerzos</div></div>" +
      "</div>" +

      "<div class='ea-adm-card'><h3>Alertas Tempranas Prioritarias <button class='ea-adm-btn is-sm' data-go-sec='alerts'>Gestionar todas</button></h3>" +
        "<div class='ea-adm-table-wrap'><table class='ea-adm-table'>" +
          "<thead><tr><th>Fecha</th><th>Curso</th><th>Categoría</th><th>Severidad</th><th>Estado</th></tr></thead><tbody>" +
          (alerts.length ? alerts.map(function (a) {
            return "<tr><td>" + esc(a.date) + "</td><td><strong>" + esc(a.course) + "</strong></td>" +
              "<td>" + esc(a.category) + "</td><td>" + esc(a.severity) + "</td><td>" + alertStatusBadge(a.status) + "</td></tr>";
          }).join("") : "<tr><td colspan='5'>No hay alertas registradas.</td></tr>") +
        "</tbody></table></div></div>" +

      "<div class='ea-adm-card'><h3>Estudiantes para Acompañamiento Preventivo <button class='ea-adm-btn is-sm' data-go-sec='students'>Ver trazabilidad</button></h3>" +
        "<div class='ea-adm-table-wrap'><table class='ea-adm-table'>" +
          "<thead><tr><th>Estudiante</th><th>Asignaturas</th><th>Prácticas</th><th>Acierto</th><th>Estado de rezago</th><th>Acción</th></tr></thead><tbody>" +
          (priorityStudents.length ? priorityStudents.map(function (s) {
            return "<tr><td><strong>" + esc(s.name) + "</strong><br><small>Meta: " + esc(s.goals || "Sin meta") + "</small></td>" +
              "<td>" + esc((s.courses || []).join(", ") || "-") + "</td>" +
              "<td>" + esc(s.attempts_count) + "</td>" +
              "<td>" + (s.accuracy_pct != null ? esc(s.accuracy_pct) + "%" : "-") + "</td>" +
              "<td>" + riskBadge(s.risk_level) + "</td>" +
              "<td><button class='ea-adm-btn is-sm is-primary' data-ori-intervene='" + esc(s.name) + "'>Registrar apoyo</button></td></tr>";
          }).join("") : "<tr><td colspan='6'>Todos los estudiantes presentan desempeño adecuado.</td></tr>") +
        "</tbody></table></div></div>"
    );
  }

  function renderStudents(d) {
    var students = d.students || [];
    var f = (state.studentFilter || "").trim().toLowerCase();
    var filtered = students.filter(function (s) {
      var matchText = !f || (s.name + " " + (s.courses || []).join(" ") + " " + s.goals).toLowerCase().indexOf(f) !== -1;
      var matchRisk = state.riskFilter === "all" || s.risk_level === state.riskFilter;
      return matchText && matchRisk;
    });

    return (
      "<div class='ea-adm-header'><div><h2>Trazabilidad Académica por Estudiante</h2>" +
      "<p>Seguimiento de prácticas, refuerzos socráticos y nivel de riesgo sin acceso a edición de pruebas.</p></div></div>" +
      "<div class='ea-adm-card'>" +
        "<div class='ea-adm-form-grid' style='margin-bottom:12px'>" +
          "<div class='ea-adm-field'><label>Buscar estudiante o asignatura</label>" +
            "<input id='ori-student-search' type='text' value='" + esc(state.studentFilter) + "' placeholder='Ej. Estudiante Demo, MAT...' /></div>" +
          "<div class='ea-adm-field'><label>Filtrar por nivel de rezago</label>" +
            "<select id='ori-risk-filter'>" +
              "<option value='all'" + (state.riskFilter === "all" ? " selected" : "") + ">Todos los niveles</option>" +
              "<option value='Alto'" + (state.riskFilter === "Alto" ? " selected" : "") + ">Riesgo Alto</option>" +
              "<option value='Medio'" + (state.riskFilter === "Medio" ? " selected" : "") + ">Riesgo Medio</option>" +
              "<option value='Bajo'" + (state.riskFilter === "Bajo" ? " selected" : "") + ">Adecuado</option>" +
              "<option value='Sin actividad'" + (state.riskFilter === "Sin actividad" ? " selected" : "") + ">Sin actividad aún</option>" +
            "</select></div>" +
        "</div>" +
        "<div class='ea-adm-table-wrap'><table class='ea-adm-table'>" +
          "<thead><tr><th>Estudiante</th><th>Estilo</th><th>Asignaturas</th><th>Intentos</th><th>Acierto</th><th>Refuerzos</th><th>Riesgo</th><th>Intervención</th></tr></thead><tbody>" +
          (filtered.length ? filtered.map(function (s) {
            return "<tr><td><strong>" + esc(s.name) + "</strong><br><small>" + esc(s.goals || "Sin meta declarada") + "</small></td>" +
              "<td>" + esc(s.presentation) + "</td>" +
              "<td>" + esc((s.courses || []).join(", ") || "-") + "</td>" +
              "<td>" + esc(s.attempts_count) + "</td>" +
              "<td>" + (s.accuracy_pct != null ? esc(s.accuracy_pct) + "%" : "-") + "</td>" +
              "<td>" + esc(s.reinforcements_count) + "</td>" +
              "<td>" + riskBadge(s.risk_level) + "</td>" +
              "<td><button class='ea-adm-btn is-sm is-primary' data-ori-intervene='" + esc(s.name) + "'>Acompañar</button></td></tr>";
          }).join("") : "<tr><td colspan='8'>Sin coincidencias.</td></tr>") +
        "</tbody></table></div></div>"
    );
  }

  function renderAlerts(d) {
    var alerts = d.early_alerts || [];
    return (
      "<div class='ea-adm-header'><div><h2>Alertas Tempranas de Bienestar Escolar</h2>" +
      "<p>Señales derivadas por los guardrails de AndesBot y seguimiento directo de Orientación (sincronizado con Consola Admin).</p></div></div>" +
      "<div class='ea-adm-card'><div class='ea-adm-table-wrap'><table class='ea-adm-table'>" +
        "<thead><tr><th>ID</th><th>Fecha</th><th>Curso</th><th>Categoría</th><th>Severidad</th><th>Estado actual</th><th>Actualizar estado</th></tr></thead><tbody>" +
        (alerts.length ? alerts.map(function (a) {
          var st = a.status || "Derivada a Orientación";
          return "<tr><td><code>" + esc(a.id) + "</code></td>" +
            "<td>" + esc(a.date) + "</td>" +
            "<td><strong>" + esc(a.course) + "</strong>" + (a.note ? "<br><small>Nota: " + esc(a.note) + "</small>" : "") + "</td>" +
            "<td>" + esc(a.category) + "</td>" +
            "<td>" + esc(a.severity) + "</td>" +
            "<td>" + alertStatusBadge(st) + "</td>" +
            "<td><div class='ea-adm-inline-actions'>" +
              "<select class='ea-ori-alert-sel' data-alert-id='" + esc(a.id) + "' style='padding:4px 8px;border-radius:6px;border:1px solid #c5d5f2'>" +
                "<option value='Derivada a Orientación'" + (st === "Derivada a Orientación" ? " selected" : "") + ">Derivada a Orientación</option>" +
                "<option value='En seguimiento'" + (st === "En seguimiento" ? " selected" : "") + ">En seguimiento</option>" +
                "<option value='Entrevista agendada'" + (st === "Entrevista agendada" ? " selected" : "") + ">Entrevista agendada</option>" +
                "<option value='Resuelta'" + (st === "Resuelta" ? " selected" : "") + ">Resuelta</option>" +
              "</select>" +
              "<button class='ea-adm-btn is-sm is-primary' data-save-alert='" + esc(a.id) + "'>Guardar</button>" +
            "</div></td></tr>";
        }).join("") : "<tr><td colspan='7'>No hay alertas tempranas registradas.</td></tr>") +
      "</tbody></table></div></div>"
    );
  }

  function renderInterventions(d) {
    var items = d.interventions || [];
    var students = d.students || [];
    var courses = d.courses || [];
    return (
      "<div class='ea-adm-header'><div><h2>Bitácora de Seguimiento e Intervenciones</h2>" +
      "<p>Registro confidencial de entrevistas, planes de refuerzo y derivaciones psicosociales.</p></div></div>" +
      "<div class='ea-adm-card'><h3>Registrar nueva intervención</h3>" +
        "<div class='ea-adm-form-grid'>" +
          "<div class='ea-adm-field'><label>Estudiante</label>" +
            "<input id='ori-int-student' list='ori-student-list' placeholder='Nombre del estudiante' value='" + esc(state.prefillStudent || "") + "' />" +
            "<datalist id='ori-student-list'>" + students.map(function (s) { return "<option value='" + esc(s.name) + "'>"; }).join("") + "</datalist></div>" +
          "<div class='ea-adm-field'><label>Asignatura / Contexto</label>" +
            "<select id='ori-int-course'>" +
              "<option value='General'>General / Transversal</option>" +
              courses.map(function (c) { return "<option value='" + esc(c.code + " · " + c.title) + "'>" + esc(c.code + " · " + c.title) + "</option>"; }).join("") +
            "</select></div>" +
          "<div class='ea-adm-field'><label>Tipo de acción</label>" +
            "<select id='ori-int-kind'>" +
              "<option value='Plan de refuerzo'>Plan de refuerzo</option>" +
              "<option value='Seguimiento académico'>Seguimiento académico</option>" +
              "<option value='Entrevista apoderado'>Entrevista con apoderado</option>" +
              "<option value='Derivación psicosocial'>Derivación psicosocial</option>" +
            "</select></div>" +
        "</div>" +
        "<div class='ea-adm-field' style='margin-top:10px'><label>Observación y acuerdo de acompañamiento</label>" +
          "<textarea id='ori-int-summary' rows='2' placeholder='Ej. Se acuerda reforzar la unidad 1 con 10 ejercicios guiados en AndesBot y revisión en 7 días.'></textarea></div>" +
        "<button class='ea-adm-btn is-primary' id='ori-btn-add-int' style='margin-top:10px'>Guardar en bitácora</button>" +
      "</div>" +

      "<div class='ea-adm-card'><h3>Historial de intervenciones (" + items.length + ")</h3>" +
        "<div class='ea-adm-table-wrap'><table class='ea-adm-table'>" +
          "<thead><tr><th>Fecha</th><th>Estudiante</th><th>Contexto</th><th>Tipo</th><th>Resumen</th><th>Responsable</th></tr></thead><tbody>" +
          (items.length ? items.map(function (it) {
            return "<tr><td><small>" + esc(it.ts) + "</small></td>" +
              "<td><strong>" + esc(it.student_name) + "</strong></td>" +
              "<td>" + esc(it.course_label) + "</td>" +
              "<td><span class='ea-adm-badge is-blue'>" + esc(it.kind) + "</span></td>" +
              "<td>" + esc(it.summary) + "</td>" +
              "<td><small>" + esc(it.author) + "</small></td></tr>";
          }).join("") : "<tr><td colspan='6'>Sin registros aún.</td></tr>") +
        "</tbody></table></div></div>"
    );
  }

  function renderCoursesAndRbac(d) {
    var courses = d.courses || [];
    return (
      "<div class='ea-adm-header'><div><h2>Cobertura Curricular y Privacidad Segregada (RBAC)</h2>" +
      "<p>Vista consolidada de asignaturas y límites estrictos de acceso para Directivo / Orientador.</p></div></div>" +
      "<div class='ea-adm-card'><h3>Asignaturas activas en plataforma</h3>" +
        "<div class='ea-adm-table-wrap'><table class='ea-adm-table'>" +
          "<thead><tr><th>Código</th><th>Asignatura</th><th>Unidades</th><th>PDFs</th><th>Páginas indexadas</th><th>Prácticas</th><th>Acierto</th></tr></thead><tbody>" +
          courses.map(function (c) {
            return "<tr><td><code>" + esc(c.code) + "</code></td><td><strong>" + esc(c.title) + "</strong></td>" +
              "<td>" + esc(c.units_count) + "</td><td>" + esc(c.materials_count) + "</td>" +
              "<td>" + esc(c.pages_count) + "</td><td>" + esc(c.attempts_count) + "</td>" +
              "<td>" + (c.accuracy_pct != null ? esc(c.accuracy_pct) + "%" : "-") + "</td></tr>";
          }).join("") +
        "</tbody></table></div></div>" +

      "<div class='ea-adm-card'><h3>Matriz de Privacidad Segregada por Rol (Directivo / Orientador)</h3>" +
        "<div class='ea-adm-grid'>" +
          "<div class='ea-adm-kpi'><div class='ea-adm-kpi-label'>Permitido en este rol</div>" +
            "<div class='ea-adm-kpi-sub' style='font-size:0.86rem;color:#0f6e40;margin-top:6px'>" +
              "• Trazabilidad académica y detección temprana de rezago<br>" +
              "• Gestión de alertas de bienestar derivadas<br>" +
              "• Bitácora confidencial de seguimiento escolar" +
            "</div></div>" +
          "<div class='ea-adm-kpi'><div class='ea-adm-kpi-label'>Bloqueado por servidor (HTTP 403)</div>" +
            "<div class='ea-adm-kpi-sub' style='font-size:0.86rem;color:#b82318;margin-top:6px'>" +
              "• No puede editar PDFs, preguntas ni calificaciones docentes<br>" +
              "• Sin acceso a Consola Admin, API Keys, cuotas ni backups<br>" +
              "• Sin acceso a conversaciones privadas de otros usuarios" +
            "</div></div>" +
        "</div></div>"
    );
  }

  function render() {
    var root = document.getElementById("ea-orientacion-console");
    if (!root) return;
    if (!state.data) {
      root.innerHTML = "<div style='padding:40px;text-align:center;font-size:1.1rem'>Cargando panel de Orientación y Bienestar...</div>";
      return;
    }

    var d = state.data;
    var u = d.user || {};
    var bodyHtml = "";
    if (state.section === "overview") bodyHtml = renderOverview(d);
    else if (state.section === "students") bodyHtml = renderStudents(d);
    else if (state.section === "alerts") bodyHtml = renderAlerts(d);
    else if (state.section === "interventions") bodyHtml = renderInterventions(d);
    else bodyHtml = renderCoursesAndRbac(d);

    root.innerHTML =
      "<header class='ea-adm-topbar'>" +
        "<div class='ea-adm-brand'><span>EduAndes AI · Orientación y Bienestar</span>" +
          "<span class='ea-adm-pill'>" + esc(u.email || sessionStorage.getItem(AUTH_EMAIL_KEY) || "") + "</span>" +
          "<span class='ea-adm-pill'>Directivo / Orientador · Solo Lectura Académica</span></div>" +
        "<div class='ea-adm-top-actions'>" +
          "<button class='ea-adm-top-btn is-logout' id='ea-ori-logout'>Cerrar sesión</button>" +
        "</div>" +
      "</header>" +
      "<div class='ea-adm-shell'>" +
        "<aside class='ea-adm-sidebar'>" +
          SECTIONS.map(function (s) {
            return "<button class='ea-adm-nav-item " + (state.section === s.id ? "is-active" : "") + "' data-ori-sec='" + s.id + "'>" + esc(s.label) + "</button>";
          }).join("") +
        "</aside>" +
        "<main class='ea-adm-main'>" + bodyHtml + "</main>" +
      "</div>" +
      (state.toast ? "<div class='ea-adm-toast'>" + esc(state.toast) + "</div>" : "");

    root.querySelectorAll("[data-ori-sec]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.section = btn.getAttribute("data-ori-sec");
        render();
      });
    });

    root.querySelectorAll("[data-go-sec]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.section = btn.getAttribute("data-go-sec");
        render();
      });
    });

    var refreshBtn = root.querySelector("#ea-ori-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", loadState);

    var logoutBtn = root.querySelector("#ea-ori-logout");
    if (logoutBtn) {
      logoutBtn.addEventListener("click", function () {
        if (typeof window.__eduandesLogout === "function") {
          window.__eduandesLogout();
        } else {
          fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }).finally(function () {
            window.location.reload();
          });
        }
      });
    }

    var searchEl = root.querySelector("#ori-student-search");
    if (searchEl) {
      searchEl.addEventListener("input", function () {
        state.studentFilter = searchEl.value;
        render();
        var el = document.querySelector("#ori-student-search");
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
      });
    }

    var riskEl = root.querySelector("#ori-risk-filter");
    if (riskEl) {
      riskEl.addEventListener("change", function () {
        state.riskFilter = riskEl.value;
        render();
      });
    }

    root.querySelectorAll("[data-ori-intervene]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.prefillStudent = btn.getAttribute("data-ori-intervene") || "";
        state.section = "interventions";
        render();
      });
    });

    root.querySelectorAll("[data-save-alert]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.getAttribute("data-save-alert");
        var sel = root.querySelector(".ea-ori-alert-sel[data-alert-id='" + id + "']");
        if (!sel) return;
        api("/api/orientacion/alerts/" + encodeURIComponent(id) + "/status", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: sel.value }),
        })
          .then(function (r) {
            state.data.early_alerts = r.early_alerts;
            state.data.kpis.active_alerts_count = (r.early_alerts || []).filter(function (a) {
              return String(a.status || "").toLowerCase().indexOf("resuelta") === -1;
            }).length;
            showToast("Estado de alerta actualizado.");
          })
          .catch(function (e) { showToast(e.message); });
      });
    });

    var addIntBtn = root.querySelector("#ori-btn-add-int");
    if (addIntBtn) {
      addIntBtn.addEventListener("click", function () {
        var studentName = (root.querySelector("#ori-int-student").value || "").trim();
        var courseLabel = root.querySelector("#ori-int-course").value;
        var kind = root.querySelector("#ori-int-kind").value;
        var summary = (root.querySelector("#ori-int-summary").value || "").trim();
        if (!studentName || summary.length < 4) {
          showToast("Completa el nombre del estudiante y el resumen de la intervención.");
          return;
        }
        api("/api/orientacion/interventions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            student_name: studentName,
            course_label: courseLabel,
            kind: kind,
            summary: summary,
          }),
        })
          .then(function (r) {
            state.data.interventions = r.interventions;
            state.data.kpis.interventions_total = r.interventions.length;
            state.prefillStudent = "";
            showToast("Intervención registrada en bitácora.");
          })
          .catch(function (e) { showToast(e.message); });
      });
    }
  }

  function mountOrUnmount() {
    var wantOri = false;
    try {
      wantOri =
        window.location.hash.indexOf("#/orientacion") === 0 ||
        sessionStorage.getItem(COUNSELOR_VIEW_KEY) === "1";
    } catch (_) {}
    var shouldShow = wantOri && isCounselorLoggedIn();
    var existing = document.getElementById("ea-orientacion-console");
    if (!shouldShow) {
      if (!document.getElementById("ea-admin-console")) {
        document.documentElement.classList.remove("ea-admin-open");
        document.body.classList.remove("ea-admin-open");
      }
      if (existing) existing.remove();
      return;
    }
    document.documentElement.classList.add("ea-admin-open");
    document.body.classList.add("ea-admin-open");
    if (!existing) {
      existing = document.createElement("div");
      existing.id = "ea-orientacion-console";
      document.body.appendChild(existing);
      loadState();
    } else {
      render();
    }
  }

  window.addEventListener("eduandes-auth-changed", mountOrUnmount);
  window.addEventListener("hashchange", mountOrUnmount);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountOrUnmount);
  } else {
    mountOrUnmount();
  }
})();
