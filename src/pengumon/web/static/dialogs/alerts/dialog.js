// Alerts: desktop notifications and sound when the verdict changes (the `diagnosis` topic), and their dialog. Kept
// in this browser (localStorage pengumon_notify / pengumon_sound).

import { registerActions } from '../../js/dispatch.js';
import { bus } from '../../js/bus.js';
import { closeModal, openModal, setBadge } from '../../js/util.js';

let notificationsEnabled = (localStorage.getItem('pengumon_notify') === 'true');
let audioAlertsEnabled = (localStorage.getItem('pengumon_sound') === 'true');
let previousDiagnosisStatus = null;

// Which changes raise an alert (this browser only). All on by default, which is what the dialog did before it had a choice.
const ALERT_KINDS = { down: 'pengumon_alert_down', degraded: 'pengumon_alert_degraded', recovered: 'pengumon_alert_recovered' };
const alertOn = (kind) => localStorage.getItem(ALERT_KINDS[kind]) !== 'false';
// The kind of alert a change of verdict is, or null for a change that never alerted (and still does not).
function alertKind(status, previous) {
  if (status === 'down' || status === 'degraded') return status;
  return status === 'healthy' && (previous === 'degraded' || previous === 'down') ? 'recovered' : null;
}

// The header button: opens the dialog, where permission, the pop-up switch, sound and the alert kinds live.
export function toggleNotifications() {
  openModal('alertsModal');
  updateAlertModalUI();
  loadServerChannels();
}

// The channels the server sends alerts to (ntfy, webhooks): names, formats and hosts only, never a URL or a secret.
async function loadServerChannels() {
  const list = document.getElementById('alChannels');
  const text = document.getElementById('alChannelsText');
  const button = document.getElementById('btnServerTest');
  if (!list || !text || !button) return;
  let channels = null;
  try {
    const res = await fetch('/api/notifications');
    if (res.ok) channels = (await res.json()).channels || [];
  } catch (_) { /* shown as "could not be read" below */ }
  list.replaceChildren();
  button.hidden = !channels || channels.length === 0;
  document.getElementById('alServerResult').textContent = '';
  if (channels === null) { text.textContent = 'The server channels could not be read.'; return; }
  text.textContent = channels.length ? 'Sent by the server, to everyone who listens, whatever this browser is set to.'
    : 'No ntfy topic or webhook is configured (see the README, Alerts).';
  for (const c of channels) {
    const li = document.createElement('li');
    const name = document.createElement('b');
    name.textContent = c.name;
    li.append(name, document.createTextNode(` ${c.kind === 'ntfy' ? 'ntfy' : `webhook, ${c.format}`}${c.host ? `, ${c.host}` : ''}`));
    list.append(li);
  }
}

// "Send a test": one message through every server channel, and what each one answered.
async function sendServerTest() {
  const button = document.getElementById('btnServerTest');
  const out = document.getElementById('alServerResult');
  button.disabled = true;
  out.textContent = 'Sending…';
  try {
    const res = await fetch('/api/notifications/test', { method: 'POST' });
    const body = res.ok ? await res.json() : null;
    out.textContent = body ? body.channels.map((c) => `${c.name}: ${c.ok ? 'delivered' : `failed (${c.detail || 'unknown'})`}`).join('; ')
      : `The test could not be sent (${res.status}).`;
  } catch (_) {
    out.textContent = 'The test could not be sent.';
  } finally {
    button.disabled = false;
  }
}

// The pop-up switch (shown once the browser allows notifications).
export function setNotificationsEnabled(on) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  notificationsEnabled = on;
  localStorage.setItem('pengumon_notify', String(on));
  updateNotifyButton();
  updateAlertModalUI();
  if (on) {
    new Notification("Pengumon Network Alerts Active", {
      body: "You will be alerted when network outages, jitter, or packet loss occur.",
      icon: "/static/icons/app-192.png"
    });
  }
}

function updateAlertModalUI() {
  const badge = document.getElementById('notifStatusBadge');
  const reqBtn = document.getElementById('btnRequestNotif');
  const soundBtn = document.getElementById('btnToggleSound');

  if (soundBtn) soundBtn.checked = audioAlertsEnabled;
  Object.keys(ALERT_KINDS).forEach((kind) => {
    const box = document.getElementById(`alert${kind[0].toUpperCase()}${kind.slice(1)}`);
    if (box) box.checked = alertOn(kind);
  });

  if (!("Notification" in window)) {
    if (badge) setBadge(badge, 'crit', 'Not available here');
    if (reqBtn) reqBtn.disabled = true;
    return;
  }

  const pop = document.getElementById('notifSwitch');
  if (pop) {
    pop.hidden = Notification.permission !== 'granted';
    pop.querySelector('input').checked = notificationsEnabled;
  }
  if (Notification.permission === 'granted') {
    if (badge) setBadge(badge, notificationsEnabled ? 'good' : 'unknown', notificationsEnabled ? 'On' : 'Off');
    if (reqBtn) { reqBtn.textContent = 'Notifications allowed'; reqBtn.disabled = true; }
  } else if (Notification.permission === 'denied') {
    if (badge) setBadge(badge, 'warn', 'Blocked');
    if (reqBtn) { reqBtn.textContent = 'Blocked in the browser settings'; reqBtn.disabled = true; }
  } else {
    if (badge) setBadge(badge, 'warn', 'Needs permission');
    if (reqBtn) { reqBtn.textContent = 'Allow notifications'; reqBtn.disabled = false; }
  }
}

export function requestDesktopPermission() {
  if (!("Notification" in window)) {
    alert("Desktop notifications are restricted by your browser over plain HTTP IP access. Use http://localhost:8000 or enable In-App Audio Chimes below!");
    return;
  }

  Notification.requestPermission().then(permission => {
    updateAlertModalUI();
    if (permission === 'granted') {
      notificationsEnabled = true;
      localStorage.setItem('pengumon_notify', 'true');
      updateNotifyButton();
      sendTestNotification();
    } else if (permission === 'denied') {
      alert("Permission was blocked. Click the lock/tune icon next to the URL in your browser address bar and toggle Notifications to 'Allow'.");
    }
  });
}

export function setAlertOption(kind, on) {
  if (!ALERT_KINDS[kind]) return;
  localStorage.setItem(ALERT_KINDS[kind], String(on));
}

export function toggleSoundAlerts() {
  audioAlertsEnabled = !audioAlertsEnabled;
  localStorage.setItem('pengumon_sound', audioAlertsEnabled);
  updateAlertModalUI();
  updateNotifyButton();
  if (audioAlertsEnabled) {
    playAlertSound('healthy', true);
  }
}

let lastNotificationTime = 0;

export function sendTestNotification() {
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      const notif = new Notification("Pengumon test alert", {
        body: "Test alert triggered! Status alerts replace each other smoothly in Windows.",
        icon: "/static/icons/app-192.png",
        tag: "pengumon-status",
        renotify: true
      });
      setTimeout(() => {
        try { notif.close(); } catch (_) {}
      }, 8000);
    } catch (e) {
      console.warn('Notification test error:', e);
    }
  } else {
    alert("Desktop notifications are not yet allowed. Follow the instructions above or enable Sound Alerts!");
  }
}

export function checkNotificationTrigger(diagnosis) {
  if (!previousDiagnosisStatus) {
    previousDiagnosisStatus = diagnosis.status;
    return;
  }

  if (diagnosis.status !== previousDiagnosisStatus) {
    const now = Date.now();
    const kind = alertKind(diagnosis.status, previousDiagnosisStatus);
    if (kind && !alertOn(kind)) { // switched off in the dialog: neither pop-up nor tone
      previousDiagnosisStatus = diagnosis.status;
      return;
    }

    // 1. Play Audio Alarm if enabled
    if (audioAlertsEnabled) {
      playAlertSound(diagnosis.status);
    }

    // 2. Send Desktop Notification if enabled
    if (notificationsEnabled && "Notification" in window && Notification.permission === "granted") {
      // Prevent spam on rapid bouncing: only send if status is 'down' or > 12s since last update
      if (diagnosis.status === 'down' || (now - lastNotificationTime) > 12000) {
        lastNotificationTime = now;
        
        let title = '';
        let body = '';

        if (diagnosis.status === 'down') {
          title = `Network outage: ${diagnosis.title}`;
          body = diagnosis.description;
        } else if (diagnosis.status === 'degraded') {
          title = `Network degraded: ${diagnosis.title}`;
          body = diagnosis.description;
        } else if (diagnosis.status === 'healthy' && (previousDiagnosisStatus === 'degraded' || previousDiagnosisStatus === 'down')) {
          title = 'Network restored: healthy';
          body = "All network hops, gateway links, and DNS resolvers have recovered.";
        }

        if (title) {
          try {
            // Using tag 'pengumon-status' replaces the existing notification card in Windows
            // rather than stacking dozens of separate cards overnight
            const notif = new Notification(title, {
              body: body,
              icon: "/static/icons/app-192.png",
              tag: "pengumon-status",
              renotify: true
            });

            // Auto-close notification after 8 seconds so it doesn't stay pinned
            setTimeout(() => {
              try { notif.close(); } catch (_) {}
            }, 8000);
          } catch (e) {
            console.warn('Error displaying notification:', e);
          }
        }
      }
    }
    previousDiagnosisStatus = diagnosis.status;
  }
}

export function playAlertSound(type, isTest = false) {

  if (!audioAlertsEnabled && !isTest) return;
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    const ctx = new AudioContext();

    if (type === 'down') {
      // Urgent double descending pulse
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(800, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(350, ctx.currentTime + 0.35);
      gain.gain.setValueAtTime(0.25, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.35);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.35);
    } else if (type === 'degraded') {
      // Warning chime (587Hz)
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(587.33, ctx.currentTime);
      gain.gain.setValueAtTime(0.2, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.3);
    } else if (type === 'healthy') {
      // Pleasant recovery chord (C5 -> E5 -> G5)
      [523.25, 659.25, 783.99].forEach((freq, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(freq, ctx.currentTime + (i * 0.09));
        gain.gain.setValueAtTime(0.12, ctx.currentTime + (i * 0.09));
        gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + (i * 0.09) + 0.35);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(ctx.currentTime + (i * 0.09));
        osc.stop(ctx.currentTime + (i * 0.09) + 0.35);
      });
    }
  } catch (e) {
    console.warn('Web Audio error:', e);
  }
}

export function updateNotifyButton() {
  const btn = document.getElementById('btnNotify');
  if (!btn) return;
  const isWebActive = (notificationsEnabled && "Notification" in window && Notification.permission === "granted");
  const isAnyActive = (isWebActive || audioAlertsEnabled);

  const parts = [];
  if (isWebActive) parts.push('Popup');
  if (audioAlertsEnabled) parts.push('Audio');
  btn.classList.toggle('is-on', isAnyActive);
  btn.title = isAnyActive ? `Alerts: on (${parts.join('+')})` : 'Alerts: off';
  btn.setAttribute('aria-label', btn.title);
}

// The actions this module's markup names (see dispatch.js).
registerActions('click', {
  toggleNotifications: () => toggleNotifications(),
  toggleNotificationsEnabled: (el) => setNotificationsEnabled(el.checked),
  requestDesktopPermission: () => requestDesktopPermission(),
  sendTestNotification: () => sendTestNotification(),
  sendServerTest: () => sendServerTest(),
  toggleSoundAlerts: () => toggleSoundAlerts(),
  playAlertSound: (el) => playAlertSound(el.dataset.arg, el.dataset.arg2 === 'true')
});
registerActions('change', {
  setAlertOption: (el) => setAlertOption(el.dataset.arg, el.checked)
});

updateNotifyButton();
bus.subscribe('diagnosis', checkNotificationTrigger);

// Keyboard: focus moves into the dialog when it opens, Tab stays inside it and Escape closes it.
export function keepKeyboard(modal, firstFocus) {
  new MutationObserver(() => {
    if (modal.classList.contains('active') && !modal.contains(document.activeElement)) (modal.querySelector(firstFocus) || modal.querySelector('.m-close')).focus();
  }).observe(modal, { attributes: true, attributeFilter: ['class'] });
  modal.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !ev.defaultPrevented) { closeModal(modal.id); return; }
    if (ev.key !== 'Tab') return;
    const items = [...modal.querySelectorAll('a[href], button, input, select, textarea, [tabindex]')].filter((e) => !e.disabled && !e.hidden && e.tabIndex >= 0 && e.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  });
}
keepKeyboard(document.getElementById('alertsModal'), '.modal-t');
