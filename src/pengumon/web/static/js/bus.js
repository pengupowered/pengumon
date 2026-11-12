// Topic bus between the live socket and the panels. live.js publishes what the server sends, panels subscribe to the
// topics they show. Each topic keeps its last value and a new subscriber gets it at once, so a panel mounted (or
// shown) later renders without waiting for the next message. Every callback runs in its own try/catch: one panel
// failing is logged and the others still get the update.

const subscribers = new Map(); // topic -> Set of callbacks
const lastValues = new Map(); // topic -> last published value
const signatures = new Map(); // topic -> JSON of what publishIfChanged last published
const every = new Map(); // topic -> how many subscribers need every change of it, also while the tab is hidden

function deliver(topic, fn, value) {
  try {
    fn(value);
  } catch (e) {
    console.error(`bus: subscriber of "${topic}" threw: ${e && e.message ? e.message : e}`);
  }
}

export const bus = {
  // Returns the function that unsubscribes. A hidden tab gets only the newest message when it shows again (live.js); a
  // subscriber that must see every change of its topic meanwhile (an alert on a status change, a list it keeps of the
  // readings it saw) passes { every: true }, and live.js then still publishes that topic while hidden (when it changed).
  subscribe(topic, fn, { every: needsEvery = false } = {}) {
    if (!subscribers.has(topic)) subscribers.set(topic, new Set());
    subscribers.get(topic).add(fn);
    if (needsEvery) every.set(topic, (every.get(topic) || 0) + 1);
    if (lastValues.has(topic)) deliver(topic, fn, lastValues.get(topic));
    return () => {
      if (subscribers.get(topic).delete(fn) && needsEvery) every.set(topic, every.get(topic) - 1);
    };
  },

  // Does a subscriber of this topic need every change of it, also while the tab is hidden?
  wantsEvery(topic) {
    return (every.get(topic) || 0) > 0;
  },

  publish(topic, value) {
    signatures.delete(topic); // (a plain publish makes the next publishIfChanged compare against nothing)
    lastValues.set(topic, value);
    // a copy, so a callback that (un)subscribes does not change who gets this message
    [...(subscribers.get(topic) || [])].forEach((fn) => deliver(topic, fn, value));
  },

  // Publishes only when the value differs from what this method published last (compared as JSON): the live message
  // repeats most of its objects every second as they were, and a subscriber should not repaint for an unchanged one.
  // Returns whether it published.
  // `skip`: top-level keys that do not count (a timestamp that moves every second).
  publishIfChanged(topic, value, skip = []) {
    const sig = JSON.stringify(skip.length && value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).filter(([k]) => !skip.includes(k))) : value);
    if (signatures.get(topic) === sig && lastValues.has(topic)) return false;
    this.publish(topic, value);
    signatures.set(topic, sig); // (after: a plain publish forgets it)
    return true;
  },

  // The last value of a topic, or undefined before the first publish.
  last(topic) {
    return lastValues.get(topic);
  }
};
