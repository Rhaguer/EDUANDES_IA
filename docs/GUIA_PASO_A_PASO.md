<p align="center">
  <img src="./assets/logos_institucionales.png" alt="TEC Cuenca · INACAP 60 Años" width="520" />
</p>
<p align="center">
  <img src="./assets/andesbot_logo.jpg" alt="AndesBot Chile – Ecuador" width="130" />
</p>

# EduAndes AI · Documentación Técnica y Guía Paso a Paso

**Hackathon Binacional Chile – Ecuador 2026** · **TEC Cuenca (Tecnológico Sudamericano) · INACAP 60 Años**  
**URL en producción (HTTPS):** https://eduandesai.duckdns.org/#/ *(alternativa: https://eduandesia.duckdns.org/#/)*

---

## 1. Arquitectura y Seguridad Implementada
- **Proxy TLS automático:** Caddy 2 + Let's Encrypt en puertos `80`/`443` hacia `127.0.0.1:8000` + `duckdns-update.timer` para actualización automática de IP tras reinicios de laboratorio AWS.
- **Contenedor Docker Multi-Stage Endurecido:** compila el **Núcleo Nativo C++20** (`eduandes_core.so` con `g++ -O2 -std=c++20 -fPIC -shared`) y ejecuta como usuario no-root `eduandes` (`UID 1000`, `cap_drop: ALL`, `no-new-privileges:true`).
- **Autenticación Argon2id + Sesiones `HttpOnly`:** `argon2-cffi` (64 MiB, `time_cost=3`), cookies `HttpOnly`, `Secure`, `SameSite=Lax` almacenadas solo como digest SHA-256 en SQLite, bloqueo anti fuerza bruta (5 intentos / 15 min) y CSRF por `Origin`.
- **Cabeceras OWASP:** `Strict-Transport-Security`, `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, `/docs` y `/openapi.json` deshabilitados (`404`).
- **Aislamiento de Privacidad y Filtro PII:** cada cuenta `Estudiante` solo puede acceder a su propio perfil en `GET /api/students` (`403` ante cualquier otro ID); RUT chileno, cédula ecuatoriana y correos se enmascaran automáticamente antes de enviar texto a Groq.

## 2. Asignaturas Precargadas (3 Asignaturas · 10 PDFs · 731 Páginas)
| Código | Asignatura | Unidad | PDFs Indexados |
|---|---|---|---|
| `EDU2026MAT` | Matemáticas | Matemáticas | `06_Probabilidad.pdf` (88 págs.) |
| `EDU2026INF` | Informática | Informática | `09_Ciberseguridad.pdf` (84 págs.), `10_Desarrollo_Web.pdf` (61 págs.) |
| `EDU2026IA` | Inteligencia Artificial | Inteligencia Artificial | `02_Machine_Learning.pdf` (39p), `04_Aprendizaje_No_Supervisado.pdf` (70p), `05_Redes_Neuronales.pdf` (64p), `07_Procesamiento_Lenguaje_Natural.pdf` (68p), `08_Vision_Artificial.pdf` (94p), `09_Sistemas_Expertos.pdf` (68p), `10_IA_Generativa.pdf` (95p) |

## 3. Credenciales Iniciales (Argon2id)
| Rol | Usuario (Correo) | Contraseña |
|---|---|---|
| **Administrador** | `administrador@ecuachile.cl` | `EduAndes#Adm2026!` |
| **Docente** | `docente@ecuachile.cl` | `EduAndes#Doc2026!` |
| **Estudiante** | `estudiante@ecuachile.cl` | `EduAndes#Est2026!` |
| **Directivo / Orientador** | `orientacion@ecuachile.cl` | `EduAndes#Ori2026!` |

## 4. Guía Paso a Paso por Rol
1. **Administrador:** ingresa con `administrador@ecuachile.cl` → gestiona cuentas Argon2id y elimina perfiles de alumnos en *1. Usuarios*, revisa gobernanza IA y Modo Evaluación en *3. Gobernanza IA*, genera backups y descarga Audit Logs en *5. Seguridad*, y revisa el estado de SQLite, Groq y C++20 en *7. Sitio Global*.
2. **Docente:** ingresa con `docente@ecuachile.cl` → selecciona cualquiera de las 3 asignaturas precargadas (`EDU2026MAT`, `EDU2026INF`, `EDU2026IA`) → genera preguntas verificadas → revisa cada cita literal y publica la práctica.
3. **Estudiante:** ingresa con `estudiante@ecuachile.cl` → practica con su perfil aislado → abre el botón flotante redondo de **AndesBot (Chile–Ecuador)** en la esquina inferior derecha para hacer cualquier pregunta sobre los PDFs con respuestas guiadas y citas verificadas.

## 5. Despliegue desde Cero
```bash
git clone https://github.com/Rhaguer/EDUANDES_IA.git && cd EDUANDES_IA
cp .env.example .env   # colocar tu EDUCORE_LLM_API_KEY de Groq
sudo docker compose up -d --build
```
La documentación extendida en Word con portadas y encabezados oficiales está en [`DOCUMENTACION_TECNICA_Y_PASO_A_PASO_EDUANDES_AI.docx`](./DOCUMENTACION_TECNICA_Y_PASO_A_PASO_EDUANDES_AI.docx).
