import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  authorizeGoogle,
  createProfile,
  deleteProfile,
  detectClaudeLogin,
  fetchCapabilities,
  fetchGoogleStatus,
  fetchProfiles,
  loginProfile,
  renameProfile,
  updateProfile,
  uploadGoogleCredentials,
} from "../api/client";
import type { DetectedLogin } from "../api/client";
import type {
  GoogleStatus,
  IntegrationStatus,
  ProfileResponse,
  ServerCapabilities,
} from "../types";

// ---------------------------------------------------------------------------
// Wizard step definitions
// ---------------------------------------------------------------------------

interface WizardStepDef {
  key: string;
  label: string;
  integrationName: string;
  required: boolean;
}

const WIZARD_STEPS: WizardStepDef[] = [
  { key: "google", label: "Google Docs", integrationName: "Google", required: true },
  { key: "exa", label: "Exa Search", integrationName: "Exa", required: false },
  { key: "claude-session", label: "Claude.ai", integrationName: "Claude.ai session", required: false },
  { key: "claude-login", label: "Agent Login", integrationName: "Claude login", required: false },
  { key: "fred", label: "FRED", integrationName: "FRED", required: false },
];

// ---------------------------------------------------------------------------
// Google setup steps
// ---------------------------------------------------------------------------

interface GoogleSetupStepDef {
  label: string;
  url: string;
  detail?: string;
  note?: string;
}

const GOOGLE_SETUP_STEPS: GoogleSetupStepDef[] = [
  {
    label: "Create a Google Cloud project",
    url: "https://console.cloud.google.com/projectcreate",
    detail: "Pick any name (e.g. \"inkwell\"). Note the project name — you'll need to select it in each following step.",
  },
  {
    label: "Enable the Google Docs API",
    url: "https://console.cloud.google.com/apis/library/docs.googleapis.com",
    note: "Make sure your project is selected in the dropdown at the top of the page.",
  },
  {
    label: "Enable the Google Drive API",
    url: "https://console.cloud.google.com/apis/library/drive.googleapis.com",
    note: "Make sure your project is selected in the dropdown at the top of the page.",
  },
  {
    label: "Configure the OAuth consent screen",
    detail: "Select External > Create, fill in app name + your email, skip scopes.",
    url: "https://console.cloud.google.com/apis/credentials/consent",
    note: "Make sure your project is selected in the dropdown at the top of the page.",
  },
  {
    label: "Add a test user",
    detail: 'Click "+ Add users" and enter the Google email you\'ll authorize with. This can be a different account than the project owner — add whichever email you\'ll sign in with during authorization.',
    url: "https://console.cloud.google.com/auth/audience",
    note: "Make sure your project is selected in the dropdown at the top of the page.",
  },
  {
    label: "Create an OAuth client ID",
    detail: 'Click "Create Credentials" > OAuth client ID > Desktop app > Create, then download the JSON.',
    url: "https://console.cloud.google.com/apis/credentials",
    note: "Make sure your project is selected in the dropdown at the top of the page.",
  },
];

// ---------------------------------------------------------------------------
// Wizard progress bar
// ---------------------------------------------------------------------------

function WizardProgress({
  steps,
  activeIndex,
  integrations,
  onJump,
}: {
  steps: WizardStepDef[];
  activeIndex: number;
  integrations: IntegrationStatus[];
  onJump: (index: number) => void;
}) {
  function isConfigured(step: WizardStepDef): boolean {
    return integrations.find((i) => i.name === step.integrationName)?.configured ?? false;
  }

  return (
    <div className="wizard-progress">
      {steps.map((step, i) => {
        const done = isConfigured(step);
        const active = i === activeIndex;
        let cls = "wizard-step-chip";
        if (done) cls += " completed";
        if (active) cls += " active";
        return (
          <button key={step.key} className={cls} onClick={() => onJump(i)} type="button">
            <span className="wizard-step-dot" />
            <span className="wizard-step-label">{step.label}</span>
            {!step.required && <span className="wizard-step-opt">opt</span>}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Copy-to-clipboard command block
// ---------------------------------------------------------------------------

function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(command).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <div className="copy-command">
      <code>{command}</code>
      <button onClick={handleCopy} type="button">
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Individual integration setup panels
// ---------------------------------------------------------------------------

function ClaudeLoginSetup({
  profile,
  integration,
  onUpdate,
}: {
  profile: string;
  integration: IntegrationStatus;
  onUpdate: () => void;
}) {
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [detected, setDetected] = useState<DetectedLogin[] | null>(null);
  const [error, setError] = useState("");
  const profileFlag = ` --profile ${profile}`;
  const command = `inkwell setup claude-login${profileFlag}`;

  async function handleSave(path: string) {
    if (!path.trim()) return;
    setSaving(true);
    setError("");
    try {
      await updateProfile(profile, { CLAUDE_CONFIG_DIR: path.trim() });
      setValue("");
      setDetected(null);
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  async function handleDetect() {
    setDetecting(true);
    setError("");
    setDetected(null);
    try {
      const result = await detectClaudeLogin(profile);
      setDetected(result.found);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Detection failed");
    } finally {
      setDetecting(false);
    }
  }

  return (
    <div className="setup-panel">
      <p className="setup-description">
        Inkwell's writing agent uses Claude Code under the hood. By default it shares
        your current login. Set up a separate account so agent usage is billed independently.
      </p>

      {integration.configured ? (
        <div className="setup-authorized">
          <span className="setup-authorized-label">Configured</span>
          <code style={{ fontSize: "0.78rem", color: "var(--accent)" }}>{integration.detail}</code>
        </div>
      ) : (
        <div className="setup-current-value">Using default (shared with Claude Code)</div>
      )}

      <p className="setup-step-intro">
        Run this command in your terminal to log in with a separate account:
      </p>
      <CopyCommand command={command} />

      <div className="setup-action-row" style={{ marginTop: 12 }}>
        <button className="btn-primary" onClick={handleDetect} disabled={detecting}>
          {detecting ? "Scanning..." : "Detect Login"}
        </button>
      </div>

      {detected !== null && detected.length === 0 && (
        <p className="setup-detect-none">
          No Claude credentials found. Run the command above first.
        </p>
      )}

      {detected !== null && detected.length > 0 && (
        <div className="setup-detect-results">
          <p className="setup-step-intro">Found {detected.length} login{detected.length > 1 ? "s" : ""}:</p>
          {detected.map((d) => (
            <div key={d.path} className="setup-detect-item">
              <div className="setup-detect-item-info">
                <span className="setup-detect-item-label">
                  {d.label}
                  {d.is_profile_match && <span className="setup-detect-match"> (match)</span>}
                </span>
                <code className="setup-detect-item-path">{d.path}</code>
              </div>
              <button
                onClick={() => handleSave(d.path)}
                disabled={saving}
                className={d.is_profile_match ? "btn-primary" : undefined}
              >
                {saving ? "Saving..." : "Use this"}
              </button>
            </div>
          ))}
        </div>
      )}

      <details className="setup-fallback">
        <summary>Or enter the config directory path manually</summary>
        <div className="setup-input-row">
          <input
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Path to CLAUDE_CONFIG_DIR"
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSave(value);
            }}
          />
          <button onClick={() => handleSave(value)} disabled={saving || !value.trim()}>
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </details>
      {error && <p className="error-message">{error}</p>}
    </div>
  );
}

function GoogleSetup({
  profile,
  refreshKey,
  onUpdate,
}: {
  profile: string;
  refreshKey: number;
  onUpdate: () => void;
}) {
  const [google, setGoogle] = useState<GoogleStatus | null>(null);
  const [uploading, setUploading] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const [error, setError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const storageKey = `inkwell-google-step-${profile}`;
  const [gcpStep, setGcpStep] = useState(() => {
    const saved = localStorage.getItem(storageKey);
    return saved !== null ? parseInt(saved, 10) : 0;
  });

  function goToGcpStep(index: number) {
    const clamped = Math.max(0, Math.min(index, GOOGLE_SETUP_STEPS.length));
    setGcpStep(clamped);
    localStorage.setItem(storageKey, String(clamped));
  }

  useEffect(() => {
    fetchGoogleStatus(profile).then(setGoogle).catch(() => {});
  }, [profile, refreshKey]);

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError("");
    try {
      const status = await uploadGoogleCredentials(profile, file);
      setGoogle(status);
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function handleAuthorize() {
    setAuthorizing(true);
    setError("");
    try {
      const status = await authorizeGoogle(profile);
      setGoogle(status);
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authorization failed");
    } finally {
      setAuthorizing(false);
    }
  }

  if (!google) return <div className="setup-panel">Loading...</div>;

  const allGcpStepsDone = gcpStep >= GOOGLE_SETUP_STEPS.length;

  return (
    <div className="setup-panel">
      <p className="setup-description">
        Google Docs is used as the live writing surface. Google Drive stores
        article drafts. Both APIs need to be enabled in a Google Cloud project.
      </p>

      {!google.has_credentials && (
        <>
          {!allGcpStepsDone ? (
            <>
              <div className="google-substep-progress">
                {GOOGLE_SETUP_STEPS.map((_, i) => (
                  <span
                    key={i}
                    className={`google-substep-dot${i < gcpStep ? " done" : ""}${i === gcpStep ? " current" : ""}`}
                    onClick={() => goToGcpStep(i)}
                  />
                ))}
              </div>

              <div className="google-substep">
                <div className="google-substep-header">
                  <span className="google-substep-number">
                    Step {gcpStep + 1} of {GOOGLE_SETUP_STEPS.length}
                  </span>
                  <h4>{GOOGLE_SETUP_STEPS[gcpStep].label}</h4>
                </div>

                {GOOGLE_SETUP_STEPS[gcpStep].detail && (
                  <p className="setup-description">{GOOGLE_SETUP_STEPS[gcpStep].detail}</p>
                )}
                {GOOGLE_SETUP_STEPS[gcpStep].note && (
                  <p className="google-substep-note">{GOOGLE_SETUP_STEPS[gcpStep].note}</p>
                )}

                <div className="google-substep-actions">
                  <a
                    href={GOOGLE_SETUP_STEPS[gcpStep].url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-primary"
                  >
                    Open in Google Cloud Console
                  </a>
                  <button onClick={() => goToGcpStep(gcpStep + 1)}>
                    Done — Next Step
                  </button>
                  {gcpStep > 0 && (
                    <button onClick={() => goToGcpStep(gcpStep - 1)} className="google-substep-back">
                      Back
                    </button>
                  )}
                </div>
              </div>
            </>
          ) : (
            <>
              <p className="setup-step-intro">
                Upload the credentials JSON you downloaded in the last step:
              </p>
              <div className="setup-file-upload">
                <input
                  ref={fileRef}
                  type="file"
                  accept=".json,application/json"
                  onChange={handleUpload}
                  disabled={uploading}
                />
                {uploading && <span className="setup-loading">Uploading...</span>}
              </div>
              <button
                className="google-substep-back"
                onClick={() => goToGcpStep(GOOGLE_SETUP_STEPS.length - 1)}
                style={{ marginTop: 8 }}
              >
                Back to setup steps
              </button>
            </>
          )}
        </>
      )}

      {google.has_credentials && !google.has_token && (
        <>
          <div className="setup-current-value">
            Credentials uploaded. Click below to authorize access to Google Docs and Drive.
          </div>
          <p className="setup-description">
            If you see "app not verified" or "access denied", make sure the Google
            account you're signing in with is listed as a{" "}
            <a
              href="https://console.cloud.google.com/auth/audience"
              target="_blank"
              rel="noopener noreferrer"
            >
              test user
            </a>{" "}
            on the project. This can be a different account than the one that owns
            the project.
          </p>
          <button
            className="btn-primary"
            onClick={handleAuthorize}
            disabled={authorizing}
          >
            {authorizing ? "Waiting for authorization..." : "Authorize Google Access"}
          </button>
        </>
      )}

      {google.has_token &&
        google.disabled_apis &&
        google.disabled_apis.length > 0 && (
          <div className="setup-current-value">
            <p className="error-message">
              Authorized — but these APIs are switched off on the project, so writing
              a Doc will fail. Enable them and retry; re-authorizing won't help (it's
              a project setting, not a token one):
            </p>
            <ul>
              {google.disabled_apis.map((api) => (
                <li key={api}>
                  <a
                    href={`https://console.cloud.google.com/apis/library/${api}`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Enable {api}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        )}

      {google.has_token &&
        !(google.disabled_apis && google.disabled_apis.length > 0) && (
          <div className="setup-authorized">
            <span className="setup-authorized-label">Authorized</span>
            <button onClick={handleAuthorize} disabled={authorizing}>
              {authorizing ? "Re-authorizing..." : "Re-authorize"}
            </button>
          </div>
        )}

      {error && <p className="error-message">{error}</p>}
    </div>
  );
}

function ApiKeySetup({
  profile,
  integration,
  envKey,
  description,
  dashboardUrl,
  dashboardLabel,
  optional,
  onUpdate,
}: {
  profile: string;
  integration: IntegrationStatus;
  envKey: string;
  description: string;
  dashboardUrl: string;
  dashboardLabel: string;
  optional?: boolean;
  onUpdate: () => void;
}) {
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function handleSave() {
    if (!value.trim()) return;
    setSaving(true);
    setError("");
    try {
      await updateProfile(profile, { [envKey]: value.trim() });
      setValue("");
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="setup-panel">
      <p className="setup-description">
        {description}
        {optional && <span className="setup-optional"> (optional)</span>}
      </p>
      <p className="setup-description">
        Get your API key from the{" "}
        <a href={dashboardUrl} target="_blank" rel="noopener noreferrer">
          {dashboardLabel}
        </a>
      </p>
      {integration.configured && (
        <div className="setup-current-value">
          Current: <code>{integration.detail}</code>
        </div>
      )}
      <div className="setup-input-row">
        <input
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={integration.configured ? "Enter new key to replace" : `Paste ${envKey}`}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleSave();
          }}
        />
        <button onClick={handleSave} disabled={saving || !value.trim()}>
          {saving ? "Saving..." : "Save"}
        </button>
      </div>
      {error && <p className="error-message">{error}</p>}
    </div>
  );
}

function ClaudeSessionSetup({
  profile,
  integration,
  capabilities,
  onUpdate,
}: {
  profile: string;
  integration: IntegrationStatus;
  capabilities: ServerCapabilities | null;
  onUpdate: () => void;
}) {
  const [loggingIn, setLoggingIn] = useState(false);
  const [cookieValue, setCookieValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const canBrowserLogin = capabilities?.has_display && capabilities?.is_local;
  const setupCommand = `inkwell setup claude --profile ${profile}`;

  async function handleLogin() {
    setLoggingIn(true);
    setError("");
    try {
      await loginProfile(profile);
      onUpdate();
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Login did not complete. Make sure to log in and wait a few seconds.",
      );
    } finally {
      setLoggingIn(false);
    }
  }

  async function handleSaveCookie() {
    if (!cookieValue.trim()) return;
    setSaving(true);
    setError("");
    try {
      await updateProfile(profile, { CLAUDE_COOKIE: cookieValue.trim() });
      setCookieValue("");
      onUpdate();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save cookie");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="setup-panel">
      <p className="setup-description">
        Used to extract conversations from Claude share links. The session is stored
        separately from your main browser.
      </p>

      {integration.configured && (
        <div className="setup-authorized">
          <span className="setup-authorized-label">Connected</span>
          <span style={{ fontSize: "0.78rem", color: "var(--text-dim)" }}>{integration.detail}</span>
        </div>
      )}

      {canBrowserLogin ? (
        <>
          <p className="setup-description">
            A dedicated browser window will open for you to log in to claude.ai.
          </p>
          <div className="setup-action-row">
            <button
              className="btn-primary"
              onClick={handleLogin}
              disabled={loggingIn}
            >
              {loggingIn
                ? "Browser open — log in and wait..."
                : integration.configured
                  ? "Re-login with Browser"
                  : "Open Browser to Login"}
            </button>
          </div>
          <details className="setup-fallback">
            <summary>Or paste a cookie manually</summary>
            <div className="setup-input-row">
              <input
                type="password"
                value={cookieValue}
                onChange={(e) => setCookieValue(e.target.value)}
                placeholder="Paste CLAUDE_COOKIE value"
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleSaveCookie();
                }}
              />
              <button onClick={handleSaveCookie} disabled={saving || !cookieValue.trim()}>
                {saving ? "Saving..." : "Save"}
              </button>
            </div>
          </details>
        </>
      ) : (
        <>
          <p className="setup-step-intro">
            Run this command in your terminal to log in with a browser:
          </p>
          <CopyCommand command={setupCommand} />
          <details className="setup-fallback">
            <summary>Or paste a cookie manually</summary>
            <div className="setup-input-row">
              <input
                type="password"
                value={cookieValue}
                onChange={(e) => setCookieValue(e.target.value)}
                placeholder="Paste CLAUDE_COOKIE value"
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleSaveCookie();
                }}
              />
              <button onClick={handleSaveCookie} disabled={saving || !cookieValue.trim()}>
                {saving ? "Saving..." : "Save"}
              </button>
            </div>
          </details>
        </>
      )}

      {error && <p className="error-message">{error}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Render a single wizard step's content
// ---------------------------------------------------------------------------

function WizardStepContent({
  step,
  profile,
  integrations,
  capabilities,
  refreshKey,
  onUpdate,
}: {
  step: WizardStepDef;
  profile: string;
  integrations: IntegrationStatus[];
  capabilities: ServerCapabilities | null;
  refreshKey: number;
  onUpdate: () => void;
}) {
  function find(name: string): IntegrationStatus {
    return integrations.find((i) => i.name === name) ?? {
      name,
      configured: false,
      detail: "not configured",
    };
  }

  switch (step.key) {
    case "google":
      return <GoogleSetup profile={profile} refreshKey={refreshKey} onUpdate={onUpdate} />;
    case "exa":
      return (
        <ApiKeySetup
          profile={profile}
          integration={find("Exa")}
          envKey="EXA_API_KEY"
          description="AI-powered web search used for deep research during article writing."
          dashboardUrl="https://dashboard.exa.ai/api-keys"
          dashboardLabel="Exa Dashboard"
          onUpdate={onUpdate}
        />
      );
    case "claude-session":
      return (
        <ClaudeSessionSetup
          profile={profile}
          integration={find("Claude.ai session")}
          capabilities={capabilities}
          onUpdate={onUpdate}
        />
      );
    case "claude-login":
      return (
        <ClaudeLoginSetup
          profile={profile}
          integration={find("Claude login")}
          onUpdate={onUpdate}
        />
      );
    case "fred":
      return (
        <ApiKeySetup
          profile={profile}
          integration={find("FRED")}
          envKey="FRED_API_KEY"
          description="Federal Reserve Economic Data — used for economics-related research in articles."
          dashboardUrl="https://fred.stlouisfed.org/docs/api/api_key.html"
          dashboardLabel="FRED API Keys"
          optional
          onUpdate={onUpdate}
        />
      );
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Profile setup — wizard mode
// ---------------------------------------------------------------------------

function ProfileWizard({
  profile,
  capabilities,
  onUpdate,
}: {
  profile: ProfileResponse;
  capabilities: ServerCapabilities | null;
  onUpdate: () => void;
}) {
  const storageKey = `inkwell-wizard-${profile.name}`;
  const [refreshKey, setRefreshKey] = useState(0);
  const [activeIndex, setActiveIndex] = useState(() => {
    const saved = localStorage.getItem(storageKey);
    if (saved !== null) {
      const idx = parseInt(saved, 10);
      if (idx >= 0 && idx < WIZARD_STEPS.length) return idx;
    }
    const firstUnconfigured = WIZARD_STEPS.findIndex(
      (s) => !profile.integrations.find((i) => i.name === s.integrationName)?.configured,
    );
    return firstUnconfigured >= 0 ? firstUnconfigured : 0;
  });
  const prevConfiguredRef = useRef(
    new Set(profile.integrations.filter((i) => i.configured).map((i) => i.name)),
  );

  const configuredKey = profile.integrations.filter((i) => i.configured).map((i) => i.name).join(",");
  useEffect(() => {
    const curr = new Set(configuredKey.split(",").filter(Boolean));
    const prev = prevConfiguredRef.current;
    prevConfiguredRef.current = curr;

    if (prev.size === 0 && curr.size === 0) return;
    if (prev.size === curr.size && [...prev].every((n) => curr.has(n))) return;

    setActiveIndex((idx) => {
      const currentStep = WIZARD_STEPS[idx];
      const justCompleted = !prev.has(currentStep.integrationName) && curr.has(currentStep.integrationName);
      if (!justCompleted || idx >= WIZARD_STEPS.length - 1) return idx;

      const next = WIZARD_STEPS.findIndex(
        (s, i) => i > idx && !curr.has(s.integrationName),
      );
      const target = next >= 0 ? next : idx + 1;
      localStorage.setItem(storageKey, String(target));
      return target;
    });
  }, [configuredKey, storageKey]);

  function goTo(index: number) {
    const clamped = Math.max(0, Math.min(index, WIZARD_STEPS.length - 1));
    setActiveIndex(clamped);
    localStorage.setItem(storageKey, String(clamped));
  }

  function handleUpdate() {
    setRefreshKey((k) => k + 1);
    onUpdate();
  }

  const step = WIZARD_STEPS[activeIndex];
  const configured = profile.integrations.filter((i) => i.configured).length;
  const total = profile.integrations.length;
  const allDone = configured === total;

  return (
    <div className="wizard-container">
      <WizardProgress
        steps={WIZARD_STEPS}
        activeIndex={activeIndex}
        integrations={profile.integrations}
        onJump={goTo}
      />

      <div className="wizard-step-header">
        <h3>
          <span className="wizard-step-number">{activeIndex + 1}/{WIZARD_STEPS.length}</span>
          {step.label}
          {!step.required && <span className="setup-optional"> (optional)</span>}
        </h3>
      </div>

      <div className="wizard-step-body">
        <WizardStepContent
          step={step}
          profile={profile.name}
          integrations={profile.integrations}
          capabilities={capabilities}
          refreshKey={refreshKey}
          onUpdate={handleUpdate}
        />
      </div>

      <div className="wizard-nav">
        <button
          onClick={() => goTo(activeIndex - 1)}
          disabled={activeIndex === 0}
        >
          Back
        </button>

        <div className="wizard-nav-right">
          {!step.required && (
            <button
              className="wizard-skip"
              onClick={() => goTo(activeIndex + 1)}
              disabled={activeIndex === WIZARD_STEPS.length - 1}
            >
              Skip
            </button>
          )}
          {activeIndex < WIZARD_STEPS.length - 1 ? (
            <button className="btn-primary" onClick={() => goTo(activeIndex + 1)}>
              Next
            </button>
          ) : (
            allDone && (
              <span className="wizard-complete-label">Setup complete</span>
            )
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Profile setup — accordion mode (improved from original)
// ---------------------------------------------------------------------------

function IntegrationSection({
  integration,
  expanded,
  onToggle,
  children,
}: {
  integration: IntegrationStatus;
  expanded: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className={`setup-section ${expanded ? "expanded" : ""}`}>
      <button className="setup-section-header" onClick={onToggle} type="button">
        <span className={`setup-status ${integration.configured ? "ok" : ""}`}>
          {integration.configured ? "+" : "-"}
        </span>
        <span className="setup-section-name">{integration.name}</span>
        <span className="setup-section-detail">{integration.detail}</span>
        <span className={`setup-chevron ${expanded ? "open" : ""}`}>v</span>
      </button>
      {expanded && <div className="setup-section-body">{children}</div>}
    </div>
  );
}

function ProfileAccordion({
  profile,
  capabilities,
  onUpdate,
}: {
  profile: ProfileResponse;
  capabilities: ServerCapabilities | null;
  onUpdate: () => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  function find(name: string): IntegrationStatus {
    return profile.integrations.find((i) => i.name === name) ?? {
      name,
      configured: false,
      detail: "not configured",
    };
  }

  function toggle(name: string) {
    setExpanded((prev) => (prev === name ? null : name));
  }

  function handleUpdate() {
    setRefreshKey((k) => k + 1);
    onUpdate();
  }

  return (
    <div className="setup-sections">
      <IntegrationSection
        integration={find("Google")}
        expanded={expanded === "Google"}
        onToggle={() => toggle("Google")}
      >
        <GoogleSetup profile={profile.name} refreshKey={refreshKey} onUpdate={handleUpdate} />
      </IntegrationSection>

      <IntegrationSection
        integration={find("Exa")}
        expanded={expanded === "Exa"}
        onToggle={() => toggle("Exa")}
      >
        <ApiKeySetup
          profile={profile.name}
          integration={find("Exa")}
          envKey="EXA_API_KEY"
          description="AI-powered web search used for deep research during article writing."
          dashboardUrl="https://dashboard.exa.ai/api-keys"
          dashboardLabel="Exa Dashboard"
          onUpdate={handleUpdate}
        />
      </IntegrationSection>

      <IntegrationSection
        integration={find("Claude.ai session")}
        expanded={expanded === "Claude.ai session"}
        onToggle={() => toggle("Claude.ai session")}
      >
        <ClaudeSessionSetup
          profile={profile.name}
          integration={find("Claude.ai session")}
          capabilities={capabilities}
          onUpdate={handleUpdate}
        />
      </IntegrationSection>

      <IntegrationSection
        integration={find("Claude login")}
        expanded={expanded === "Claude login"}
        onToggle={() => toggle("Claude login")}
      >
        <ClaudeLoginSetup
          profile={profile.name}
          integration={find("Claude login")}
          onUpdate={handleUpdate}
        />
      </IntegrationSection>

      <IntegrationSection
        integration={find("FRED")}
        expanded={expanded === "FRED"}
        onToggle={() => toggle("FRED")}
      >
        <ApiKeySetup
          profile={profile.name}
          integration={find("FRED")}
          envKey="FRED_API_KEY"
          description="Federal Reserve Economic Data — used for economics-related research in articles."
          dashboardUrl="https://fred.stlouisfed.org/docs/api/api_key.html"
          dashboardLabel="FRED API Keys"
          optional
          onUpdate={handleUpdate}
        />
      </IntegrationSection>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Profile card — wraps wizard or accordion with header
// ---------------------------------------------------------------------------

function ProfileCard({
  profile,
  capabilities,
  onUpdate,
}: {
  profile: ProfileResponse;
  capabilities: ServerCapabilities | null;
  onUpdate: () => void;
}) {
  const configured = profile.integrations.filter((i) => i.configured).length;
  const total = profile.integrations.length;
  const mostlyDone = configured >= 3;

  const [viewMode, setViewMode] = useState<"wizard" | "list">(() =>
    mostlyDone ? "list" : "wizard",
  );
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function handleRename() {
    const name = renameValue.trim();
    if (!name) return;
    setSaving(true);
    try {
      await renameProfile(profile.name, name);
      setRenaming(false);
      onUpdate();
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!window.confirm(`Delete profile "${profile.name}"?`)) return;
    setDeleting(true);
    try {
      await deleteProfile(profile.name);
      onUpdate();
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="profile-setup">
      <div className="profile-card-header">
        {renaming ? (
          <span className="profile-rename-row">
            <input
              type="text"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              placeholder="New name"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === "Enter") handleRename();
                if (e.key === "Escape") setRenaming(false);
              }}
            />
            <button onClick={handleRename} disabled={saving}>Save</button>
            <button onClick={() => setRenaming(false)}>Cancel</button>
          </span>
        ) : (
          <h3>
            {profile.name}
            <span className="profile-setup-progress">
              {configured}/{total}
            </span>
            <button
              className="profile-edit-btn"
              onClick={() => {
                setRenaming(true);
                setRenameValue(profile.name);
              }}
            >
              Rename
            </button>
          </h3>
        )}
        <div className="profile-card-actions">
          <div className="view-toggle">
            <button
              className={viewMode === "wizard" ? "active" : ""}
              onClick={() => setViewMode("wizard")}
              type="button"
            >
              Setup
            </button>
            <button
              className={viewMode === "list" ? "active" : ""}
              onClick={() => setViewMode("list")}
              type="button"
            >
              All
            </button>
          </div>
          <button className="btn-danger" onClick={handleDelete} disabled={deleting}>
            Delete
          </button>
        </div>
      </div>

      {viewMode === "wizard" ? (
        <ProfileWizard profile={profile} capabilities={capabilities} onUpdate={onUpdate} />
      ) : (
        <ProfileAccordion profile={profile} capabilities={capabilities} onUpdate={onUpdate} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Settings page
// ---------------------------------------------------------------------------

export function Settings() {
  const [profiles, setProfiles] = useState<ProfileResponse[]>([]);
  const [capabilities, setCapabilities] = useState<ServerCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  const loadRef = useRef<boolean | null>(null);

  async function load() {
    try {
      const [data, caps] = await Promise.all([fetchProfiles(), fetchCapabilities()]);
      setProfiles(data);
      setCapabilities(caps);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load profiles");
    } finally {
      setLoading(false);
    }
  }

  if (loadRef.current == null) {
    loadRef.current = true;
    load();
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    setError("");
    try {
      await createProfile(name);
      setNewName("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create profile");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="page settings-page">
      <div className="page-header">
        <h1>Settings</h1>
        <Link to="/" className="back-link">
          Sessions
        </Link>
      </div>

      <section>
        <h2>Profiles</h2>
        <p className="settings-hint">
          Each profile stores its own API keys, Google OAuth tokens, and sessions.
          The wizard walks you through each integration step by step.
        </p>

        {loading && <p>Loading...</p>}

        {profiles.map((p) => (
          <ProfileCard
            key={p.name}
            profile={p}
            capabilities={capabilities}
            onUpdate={load}
          />
        ))}

        {!loading && profiles.length === 0 && (
          <p className="empty-state">No profiles configured yet.</p>
        )}

        <form className="create-profile-form" onSubmit={handleCreate}>
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="New profile name"
            pattern="[a-zA-Z0-9_-]+"
            title="Letters, numbers, hyphens, underscores"
          />
          <button
            type="submit"
            className="btn-primary"
            disabled={creating || !newName.trim()}
          >
            Create
          </button>
        </form>
        {error && <p className="error-message">{error}</p>}
      </section>
    </div>
  );
}
