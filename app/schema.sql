-- =========================================================
--  IT Helpdesk Platform schema (PostgreSQL 16)
--  Run by every service at start-up; idempotent.
--  Table ownership by service:
--    tickets:       tickets, ticket_comments, ticket_events
--    sla:           (reads tickets; owns the breach flags)
--    notifications: notifications
--    chatbot:       kb_articles
-- =========================================================
SELECT pg_advisory_xact_lock(515151);   -- one pod at a time runs the DDL (released at COMMIT)

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id        SERIAL PRIMARY KEY,
    requester_id     TEXT NOT NULL,                 -- Keycloak "sub" of the employee
    requester_name   TEXT NOT NULL,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL,
    category         TEXT NOT NULL CHECK (category IN
                        ('Network', 'Hardware', 'Software', 'Access', 'Email', 'Security', 'Other')),
    priority         TEXT NOT NULL CHECK (priority IN ('P1', 'P2', 'P3', 'P4')),
    status           TEXT NOT NULL DEFAULT 'OPEN'
                        CHECK (status IN ('OPEN', 'IN_PROGRESS', 'WAITING_ON_USER', 'RESOLVED', 'CLOSED')),
    assignee_id      TEXT,
    assignee_name    TEXT,
    ai_suggestion    TEXT NOT NULL DEFAULT '',
    triaged_by       TEXT NOT NULL DEFAULT 'rules' CHECK (triaged_by IN ('ai', 'rules', 'agent')),
    response_due_at  TIMESTAMPTZ NOT NULL,
    resolve_due_at   TIMESTAMPTZ NOT NULL,
    first_response_at TIMESTAMPTZ,
    resolved_at      TIMESTAMPTZ,
    response_breached BOOLEAN NOT NULL DEFAULT false,
    resolve_breached  BOOLEAN NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tickets_requester_idx ON tickets (requester_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tickets_queue_idx ON tickets (status, priority, created_at);

CREATE TABLE IF NOT EXISTS ticket_comments (
    comment_id   SERIAL PRIMARY KEY,
    ticket_id    INT NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    author_id    TEXT NOT NULL,
    author_name  TEXT NOT NULL,
    body         TEXT NOT NULL,
    internal     BOOLEAN NOT NULL DEFAULT false,      -- internal notes are hidden from the requester
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit trail: every change to a ticket is recorded (who, what, when)
CREATE TABLE IF NOT EXISTS ticket_events (
    event_id     SERIAL PRIMARY KEY,
    ticket_id    INT NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id SERIAL PRIMARY KEY,
    recipient_id    TEXT,                  -- a specific user, or NULL when sent to a role
    recipient_role  TEXT,                  -- e.g. 'agent'
    ticket_id       INT,
    message         TEXT NOT NULL,
    read            BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (recipient_id IS NOT NULL OR recipient_role IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS notifications_recipient_idx ON notifications (recipient_id, created_at DESC);

CREATE TABLE IF NOT EXISTS kb_articles (
    article_id  SERIAL PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    category    TEXT NOT NULL,
    body        TEXT NOT NULL,
    search      TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || body)) STORED
);
CREATE INDEX IF NOT EXISTS kb_search_idx ON kb_articles USING GIN (search);

INSERT INTO kb_articles (slug, title, category, body) VALUES
 ('vpn-not-connecting', 'VPN will not connect', 'Network',
  'Check your internet connection first. Quit and reopen the VPN client, then sign in again with your company account and approve the MFA prompt. If you see "certificate expired", update the VPN client from the Software Center. Still failing? Raise a Network ticket with a screenshot of the error.'),
 ('reset-password', 'Reset a forgotten password', 'Access',
  'Use the "Forgot password" link on the company sign-in page. You will need your registered phone for the verification code. Passwords must be at least 12 characters with a number and a symbol. Accounts lock for 15 minutes after 5 failed attempts.'),
 ('setup-mfa', 'Set up multi-factor authentication (MFA)', 'Security',
  'Install Google Authenticator or Microsoft Authenticator on your phone. On your next sign-in, scan the QR code shown and enter the 6-digit code. Keep backup codes somewhere safe. Lost your phone? Raise a P2 Access ticket so an agent can reset your MFA.'),
 ('outlook-not-syncing', 'Outlook is not syncing email', 'Email',
  'Check that Outlook shows "Connected" at the bottom. Restart Outlook. If the mailbox is full (over 50 GB), archive old mail. Remove and re-add the account if sync is still stuck. Phishing emails must be reported with the Report Phishing button, not forwarded.'),
 ('printer-offline', 'Printer shows offline', 'Hardware',
  'Make sure the printer is on and connected to the office Wi-Fi. Remove the printer from Settings > Printers and add it again using its floor name, for example FLOOR2-PRINTER. Paper jams and toner are handled by the facilities team.'),
 ('laptop-slow', 'Laptop is slow', 'Hardware',
  'Restart the laptop (many issues clear after a restart). Close unused browser tabs and apps. Check that disk space is above 10 percent free. Install pending Windows updates. If the fan is loud and the laptop is hot, raise a Hardware ticket.'),
 ('install-software', 'Install approved software', 'Software',
  'Open the Software Center and search for the application. Approved software installs without admin rights. For software not in the catalogue, raise a Software ticket with the business reason; licences need manager approval.'),
 ('wifi-issues', 'Office Wi-Fi problems', 'Network',
  'Connect to CORP-WIFI, not GUEST-WIFI. Forget the network and reconnect with your company credentials. If many people on the same floor are affected, raise a P2 Network ticket so the network team can check the access point.'),
 ('suspicious-email', 'Received a suspicious email', 'Security',
  'Do not click links or open attachments. Use the Report Phishing button in Outlook. If you already clicked a link or entered your password, change your password immediately and raise a P1 Security ticket.')
ON CONFLICT (slug) DO NOTHING;
