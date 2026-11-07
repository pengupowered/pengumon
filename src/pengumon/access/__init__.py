"""Who may open the dashboard: the sign-in password (homeguard's access token), the first-run question and HTTPS.

manager.py holds the password and the question, settings.py the viewers setting (`access.allow_viewers`), tls.py
the optional certificate (PENGUMON_TLS_CERT / PENGUMON_TLS_KEY). docs/security.md describes them for users.
"""
