import { useState } from 'react'
import { api, setToken, AuthError } from './api'
import { ThemeToggle } from './ThemeToggle'
import type { Theme } from './theme'
import type { Me } from './types'

/**
 * Auth gate. Login takes a username-or-email identifier + password; signup
 * takes username + email + password + the licence key the client bought (which
 * activates the account instantly). On success it stores the JWT and calls
 * onAuthed() so the app can load the authenticated dashboard.
 */
export function Login({
  onAuthed,
  theme,
  onToggleTheme,
}: {
  onAuthed: () => void
  theme: Theme
  onToggleTheme: () => void
}) {
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  // Login uses a single identifier (username OR email). Signup collects all
  // three plus the licence key the client bought.
  const [identifier, setIdentifier] = useState('')
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [licenseKey, setLicenseKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (mode === 'login') {
      if (!identifier.trim() || !password) {
        setError('Enter your username or email, and your password.')
        return
      }
    } else {
      if (!username.trim() || !email.trim() || !password) {
        setError('Username, email and password are all required.')
        return
      }
      if (!/^[A-Za-z0-9._-]{3,64}$/.test(username.trim())) {
        setError('Username: 3–64 chars, letters/numbers/. _ - only.')
        return
      }
      if (password.length < 8) {
        setError('Password must be at least 8 characters.')
        return
      }
      // The licence key is required for normal clients but the operator/admin
      // (first user or ADMIN_EMAIL) is exempt server-side — so we don't hard-block
      // an empty key here; the server returns a precise error if one is needed.
    }
    setBusy(true)
    try {
      const res =
        mode === 'login'
          ? await api.login(identifier.trim(), password)
          : await api.signup({
              username: username.trim(),
              email: email.trim(),
              password,
              license_key: licenseKey.trim(),
            })
      setToken(res.access_token)
      onAuthed()
    } catch (err) {
      const msg = err instanceof AuthError || err instanceof Error ? err.message : 'Failed'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-top">
          <div className="brand" style={{ fontSize: 20 }}>
            <span className="dot" />
            Trading-track
          </div>
          <ThemeToggle theme={theme} onToggle={onToggleTheme} />
        </div>
        <p className="auth-tagline">Real-time crypto trading terminal</p>
        <p className="hint">
          {mode === 'login'
            ? 'Sign in with your username or email.'
            : 'Create your account with the licence key you were given — you go live the moment you sign up.'}
        </p>
        {mode === 'login' ? (
          <div className="field">
            <label>Username or email</label>
            <input
              className="input"
              type="text"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              autoComplete="username"
              placeholder="your username or you@example.com"
            />
          </div>
        ) : (
          <>
            <div className="field">
              <label>Username</label>
              <input
                className="input"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                placeholder="pick a username"
                spellCheck={false}
              />
            </div>
            <div className="field">
              <label>Email</label>
              <input
                className="input"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                placeholder="you@example.com"
              />
            </div>
          </>
        )}
        <div className="field">
          <label>Password</label>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            placeholder="at least 8 characters"
          />
        </div>
        {mode === 'signup' && (
          <div className="field">
            <label>Licence key</label>
            <input
              className="input mono"
              value={licenseKey}
              onChange={(e) => setLicenseKey(e.target.value)}
              placeholder="TT-… (from your provider)"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
        )}
        {error && <p className="hint" style={{ color: 'var(--red)' }}>{error}</p>}
        <button className="btn primary" type="submit" disabled={busy}>
          {busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Sign up'}
        </button>
        <p className="hint" style={{ marginTop: 4 }}>
          {mode === 'login' ? (
            <>
              No account?{' '}
              <a onClick={() => { setMode('signup'); setError('') }} style={{ cursor: 'pointer' }}>
                Sign up
              </a>
            </>
          ) : (
            <>
              Already registered?{' '}
              <a onClick={() => { setMode('login'); setError('') }} style={{ cursor: 'pointer' }}>
                Sign in
              </a>
            </>
          )}
        </p>
      </form>
    </div>
  )
}

/** Shown to a logged-in user who has no effective access: pending, expired
 * (time-limited licence ran out) or revoked. Pending/expired can redeem a key;
 * revoked cannot (admin-only restore). */
export function LicenseGate({
  status,
  email,
  onLogout,
  onRedeemed,
  theme,
  onToggleTheme,
}: {
  status: 'pending' | 'revoked' | 'expired'
  email: string
  onLogout: () => void
  onRedeemed: (me: Me) => void
  theme: Theme
  onToggleTheme: () => void
}) {
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const canRedeem = status !== 'revoked' // pending & expired may (re)activate

  const redeem = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (!key.trim()) {
      setError('Paste the licence key your administrator gave you.')
      return
    }
    setBusy(true)
    try {
      const me = await api.redeemLicenseKey(key.trim())
      onRedeemed(me) // flips to active; app shows the dashboard
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not redeem that key.')
    } finally {
      setBusy(false)
    }
  }

  const heading =
    status === 'pending'
      ? 'Activate your account'
      : status === 'expired'
        ? 'Licence expired'
        : 'Licence revoked'
  const blurb =
    status === 'pending'
      ? `Your account (${email}) is registered but not yet licensed. Enter a licence key to activate instantly, or wait for an administrator to grant access.`
      : status === 'expired'
        ? `Your licence for ${email} has run out. Enter a new licence key to renew instantly, or ask the administrator to add more days.`
        : `Your licence has been revoked. Contact the administrator to restore access — a licence key can't reactivate a revoked account.`

  return (
    <div className="auth-wrap">
      <div className="auth-card">
        <div className="auth-top">
          <div className="brand" style={{ fontSize: 20 }}>
            <span className="dot" />
            Trading-track
          </div>
          <ThemeToggle theme={theme} onToggle={onToggleTheme} />
        </div>
        <p className="auth-tagline">Real-time crypto trading terminal</p>
        <h3 style={{ margin: '8px 0' }}>{heading}</h3>
        <p className="hint">{blurb}</p>
        {canRedeem && (
          <form onSubmit={redeem} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div className="field">
              <label>Licence key</label>
              <input
                className="input mono"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="TT-…"
                autoComplete="off"
                spellCheck={false}
              />
            </div>
            {error && <p className="hint" style={{ color: 'var(--red)' }}>{error}</p>}
            <button className="btn primary" type="submit" disabled={busy}>
              {busy ? 'Activating…' : status === 'expired' ? 'Renew with key' : 'Activate with key'}
            </button>
          </form>
        )}
        <button className="btn" onClick={onLogout} style={{ marginTop: 10 }}>
          Sign out
        </button>
      </div>
    </div>
  )
}
