// Topic bus between the live socket and the panels. live.js publishes what the server sends, panels subscribe to the
// topics they show. Each topic keeps its last value and a new subscriber gets it at once, so a panel mounted (or
// shown) later renders without waiting for the next message. Every callback runs in its own try/catch: one panel
// failing is logged and the others still get the update.

const subscribers = new Map(); // topic -> Set of callbacks
const lastValues = new Map(); // topic -> last published value

function deliver(topic, fn, value) {
  try {
    fn(value);
  } catch (e) {
    console.error(`bus: subscriber of "${topic}" threw: ${e && e.message ? e.message : e}`);
  }
}

export const bus = {
  // Returns the function that unsubscribes.
  subscribe(topic, fn) {
    if (!subscribers.has(topic)) subscribers.set(topic, new Set());
    subscribers.get(topic).add(fn);
    if (lastValues.has(topic)) deliver(topic, fn, lastValues.get(topic));
    return () => subscribers.get(topic).delete(fn);
  },

  publish(topic, value) {
    lastValues.set(topic, value);
    // a copy, so a callback that (un)subscribes does not change who gets this message
    [...(subscribers.get(topic) || [])].forEach((fn) => deliver(topic, fn, value));
  },

  // The last value of a topic, or undefined before the first publish.
  last(topic) {
    return lastValues.get(topic);
  }
};
