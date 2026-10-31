# DMG Assignment Hub (backend service)
FastAPI + PostgreSQL, Google Workspace sign-in, Docker. Region walls are enforced on the server for every request.

## How access works
- **Employees** have a **title** (Junior Analyst, Analyst, Senior Analyst, Associate, Senior Associate), one or more **regions**, an optional **Reviewer tag**, and optionally the **Admin** flag.
- **Admins:** set `ADMIN_EMAIL` to one or more comma-separated emails (e.g. `hardik.maheshwari@gain.ai,devashish.bhardwaj@gain.ai`). Each is created as an Admin on first start, with access to every region. More admins can be added later on the Team page.
- **Region wall:** people only ever receive, see and count assets from their own regions. Asking for another region returns 403. Admins see every region and are the only ones who can give people regions.
- **Assignment:** Get Next pulls only same-region assets (then profile-type eligibility, ownership priority, oldest Request update from). Admin reassignment is also same-region only; there is no override for region.
- **Review pipeline:** completed updates go to the review pool of the asset's region. Only people with the Reviewer tag (and admins) can see it, only for their regions. Reviewers **pick** from the pool; admins can also **assign** (to a reviewer in that region). Nobody can review their own update. Scoring uses the sheet-based QA model.
- **CMS link:** every asset row links to the CMS using the template in Settings (`https://cms.example.com/company/{asset_id}`), or a per-asset `CMS link` column in the CSV. Only http/https links are accepted.
- **Assets with a region outside the allowed list** are skipped on import (reported) and never assignable.

## Team CSV (Team page)
`Name, Email, Assigned Roles, Region, Eligibility`. Assigned Roles = one title and/or `Reviewer` / `Admin` (e.g. `Senior Analyst, Reviewer`). Region = one or several allowed regions (`DACH; France`). Eligibility = profile types only (`Full, Limited`; blank = all). A preview is shown before saving.

## Run locally (no Google needed)
    pip install -r requirements.txt
    DEV_MODE=true ADMIN_EMAIL=you@x.com uvicorn app.main:app --reload
    open http://localhost:8000      # /auth/dev?email=... switches user
    pytest

## Deploy
See the earlier steps: Google OAuth client, `.env` from `.env.example`, `docker compose up -d --build`, HTTPS in front.
**Upgrading from the previous version:** the user and asset tables changed (availability, updater readings, export tables), and `openpyxl` is a new dependency. Start with a fresh database. Start with a fresh database (delete `dmg.db`, or create a new Postgres database); there are no migrations yet.

## Added in this version
- **Assets nobody can take** (admin menu "Can't be taken", plus a daily alert): available assets that no active, eligible person in the asset's region can receive, shown as *no eligible person*, *everyone away today* or *waiting for capacity* (normal), each with the reason and the fix.
- **Weighted output and team stats** (admin menu "Team stats"; non-admins see only their own row): points per finished update = profile-type weight x (1 + complexity bonus). The weights, the complexity bands and points per review are editable by admins in **Settings > Weighted output** and apply immediately to all history. Counted by the date work was finished. The defaults (Full 1.0, Limited 0.75, Minimal 0.5, bonus +0/25/50% by difficulty band, 0.4 per review) are placeholders.
- **Updater vs reviewer difficulty readings:** when an updater clicks Mark Complete they re-enter ownership / type / deals. The reviewer enters their own and sees the updater's beside it, with a live "differs / matches" tag. The reviewer's reading wins once it exists. Team stats shows how often readings differ and the average gap.
- **Search and filters over the whole history:** by text (ID, name; admins also notes and people), region, status, ownership, profile type, person, and date. Admins see everything; everyone else only their own regions, with other people's QA details hidden.
- **Exports** (admins): filtered CSV or Excel (optional tab per region). Internal columns (QA %, difficulty, weighted points, time spent, comments) are off unless ticked. Saved export profiles are shared by all admins, and every export is logged (who, filters, rows, format).
- **Who is on:** availability (Available / Not available / On leave) with a note, plus open items and capacity per person and free slots per region. Anyone not Available cannot use Get Next. People can set themselves Available / Not available; only admins can mark On leave. The leave calendar also counts as On leave.
- **Screens brought across from the React prototype:** KPI, Quality, Manager Overview (charts), Projects, Leave planning and Alerts, all backed by the same API.

## Not in this version
Shifts (availability only for now), integrations with other tools, database migrations. The screens above are rebuilt in this app's own interface rather than reusing the React code, which is wired to an in-browser engine.
