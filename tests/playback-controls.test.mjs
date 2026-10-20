import test from 'node:test';
import assert from 'node:assert/strict';
import { SpeedControl, AdvancedPlaybackOptions, RecommendationDisclosure, formatPlaybackRate, normalizePlaybackRate, snapPlaybackRate, normalizeReverb } from '../public/playback-controls.js';

class Node {
  constructor(document, parent = null) {
    this.ownerDocument = document || this;
    this.parent = parent;
    this.listeners = new Map();
    this.attributes = new Map();
    this.children = [];
    this.selectors = new Map();
    this.classes = new Set();
    this.classList = { add: value => this.classes.add(value), remove: value => this.classes.delete(value), toggle: (value, enabled) => enabled ? this.classes.add(value) : this.classes.delete(value) };
    this.style = { setProperty() {} };
    this.hidden = false;
    this.value = '';
    this.checked = false;
    this.focusCalls = 0;
    parent?.children.push(this);
  }
  addEventListener(type, listener) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(listener); }
  removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name); }
  querySelector(selector) { return this.selectors.get(selector) || null; }
  querySelectorAll(selector) { const result = this.selectors.get(selector); return Array.isArray(result) ? result : result ? [result] : []; }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  focus() {
    const previous = this.ownerDocument.activeElement;
    this.ownerDocument.activeElement = this;
    this.focusCalls++;
    if (previous && previous !== this) previous.fire('focusout', { relatedTarget: this });
  }
  fire(type, properties = {}) {
    const event = { type, target: this, defaultPrevented: false, stopped: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...properties };
    let node = this;
    while (node) {
      for (const listener of node.listeners.get(type) || []) listener(event);
      if (event.stopped) break;
      node = node.parent;
    }
    return event;
  }
}

function fixture(kind = 'speed', options = {}) {
  const document = new Node();
  document.defaultView = { matchMedia: () => ({ matches: false }) };
  const root = new Node(document, document), trigger = new Node(document, root), panel = new Node(document, root), range = new Node(document, panel), pitch = new Node(document, panel), readout = new Node(document, trigger), expandedReadout = new Node(document, panel), effectsStatus = new Node(document, panel), outside = new Node(document, document);
  const rates = [], pitches = [], reverbs = [];
  for (const [selector, value] of Object.entries({ '[data-speed-toggle]': trigger, '[data-speed-panel]': panel, '[data-speed-range]': range, '[data-speed-pitch]': pitch, '[data-speed-readout]': [readout, expandedReadout], '[data-advanced-toggle]': trigger, '[data-advanced-panel]': panel, '[data-reverb-range]': range, '[data-reverb-readout]': readout, '[data-effects-status]': effectsStatus })) root.selectors.set(selector, value);
  const control = kind === 'speed' ? new SpeedControl({ root, onPlaybackRateChange: value => rates.push(value), onPitchChangesChange: value => pitches.push(value), ...options }) : new AdvancedPlaybackOptions({ root, onReverbChange: value => reverbs.push(value), ...options });
  return { document, root, trigger, panel, range, pitch, readout, expandedReadout, effectsStatus, outside, rates, pitches, reverbs, control };
}

test('speed formatting has useful precision without trailing zeroes', () => {
  assert.deepEqual([0.5, 0.75, 1, 1.25, 1.37, 1.5, 2].map(formatPlaybackRate), ['0.5×', '0.75×', '1×', '1.25×', '1.37×', '1.5×', '2×']);
  assert.equal(normalizePlaybackRate(0), 0.5);
  assert.equal(normalizePlaybackRate(5), 2);
  assert.equal(normalizePlaybackRate('invalid'), 1);
  assert.equal(normalizePlaybackRate(1.374), 1.37);
});

test('pointer snapping only covers a small area around familiar rates', () => {
  for (const [input, expected] of [[0.51,0.5], [0.74,0.75], [0.99,1], [1.26,1.25], [1.49,1.5], [1.76,1.75], [1.99,2], [1.37,1.37], [1.22,1.22], [1.28,1.28]]) assert.equal(snapPlaybackRate(input), expected);
});

test('speed starts compact with pitch following speed and opens into a focused native range', () => {
  const f = fixture();
  assert.equal(f.trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(f.panel.hidden, true);
  assert.equal(f.pitch.checked, true);
  assert.equal(f.readout.textContent, '1×');
  f.trigger.focus(); f.trigger.fire('click');
  assert.equal(f.trigger.getAttribute('aria-expanded'), 'true');
  assert.equal(f.panel.hidden, false);
  assert.equal(f.panel.inert, false);
  assert.equal(f.document.activeElement, f.range);
  assert.deepEqual(f.rates, []);
});

test('pointer drag snaps gently, escapes the snap zone, and closes only on its own release', () => {
  const f = fixture(); f.control.open();
  f.range.fire('pointerdown', { pointerId: 8, button: 0 });
  f.range.value = '1.24'; f.range.fire('input'); assert.equal(f.range.value, '1.25');
  f.range.value = '1.28'; f.range.fire('input'); assert.equal(f.range.value, '1.28');
  f.outside.fire('pointerdown', { pointerId: 99 }); assert.equal(f.control.expanded, true);
  f.document.fire('pointerup', { pointerId: 99 }); assert.equal(f.control.expanded, true);
  f.document.fire('pointerup', { pointerId: 8 });
  assert.equal(f.control.expanded, false); assert.equal(f.panel.hidden, true);
  assert.equal(f.document.activeElement, f.trigger);
  assert.deepEqual(f.rates, [1.25, 1.28]);
});

test('native keyboard fine steps stay open and never get trapped at a preset', () => {
  const f = fixture(); f.control.open();
  for (const value of ['1.01', '1.02', '1.03', '1.24', '1.25', '1.26']) {
    f.range.value = value; f.range.fire('input'); f.range.fire('change');
    assert.equal(f.range.value, value); assert.equal(f.document.activeElement, f.range); assert.equal(f.control.expanded, true);
  }
  assert.deepEqual(f.rates, [1.01,1.02,1.03,1.24,1.25,1.26]);
});

test('Enter and Escape close and return focus without an extra playback change', () => {
  for (const key of ['Enter', 'Escape']) {
    const f = fixture(); f.control.open();
    const event = f.range.fire('keydown', { key });
    assert.equal(event.defaultPrevented, true); assert.equal(event.stopped, true);
    assert.equal(f.document.activeElement, f.trigger); assert.equal(f.control.expanded, false); assert.deepEqual(f.rates, []);
  }
});

test('outside clicks close without stealing the outside control focus', () => {
  const f = fixture(); f.control.open();
  f.outside.focus(); f.outside.fire('pointerdown');
  assert.equal(f.control.expanded, false); assert.equal(f.document.activeElement, f.outside);
  assert.deepEqual(f.rates, []);
});

test('Tab between speed and pitch stays open; leaving the disclosure closes it', () => {
  const f = fixture(); f.control.open(); f.pitch.focus();
  assert.equal(f.control.expanded, true);
  f.outside.focus(); assert.equal(f.control.expanded, false);
  assert.equal(f.document.activeElement, f.outside);
});

test('a cancelled touch drag releases snapping and lets keyboard interaction continue', () => {
  const f = fixture(); f.control.open(); f.range.fire('pointerdown', { pointerId: 2, button: 0 });
  f.document.fire('pointercancel', { pointerId: 2 });
  f.range.value = '1.01'; f.range.fire('input');
  assert.equal(f.range.value, '1.01'); assert.equal(f.control.expanded, true);
});

test('keyboard input regains fine precision even if a pointer release was lost', () => {
  const f = fixture(); f.control.open(); f.range.fire('pointerdown', { pointerId: 2, button: 0 });
  f.range.fire('keydown', { key: 'ArrowRight' });
  f.range.value = '1.01'; f.range.fire('input');
  assert.equal(f.range.value, '1.01'); assert.equal(f.control.expanded, true);
});

test('shared settings update readouts and pitch without replaying a user callback', () => {
  const f = fixture(); f.control.open();
  f.control.setPlaybackSettings({ playbackRate: 1.37, pitchChanges: false });
  assert.equal(f.range.value, '1.37'); assert.equal(f.pitch.checked, false);
  assert.equal(f.readout.textContent, '1.37×'); assert.equal(f.expandedReadout.textContent, '1.37×');
  assert.equal(f.range.getAttribute('aria-valuetext'), '1.37× speed');
  assert.deepEqual(f.rates, []); assert.deepEqual(f.pitches, []); assert.equal(f.control.expanded, true);
  f.pitch.checked = true; f.pitch.fire('change'); assert.deepEqual(f.pitches, [true]);
});

test('advanced reverb starts off, keeps focus through adjustments, and reflects shared state', () => {
  const f = fixture('advanced');
  assert.equal(f.range.value, '0'); assert.equal(f.readout.textContent, 'Off');
  f.control.open(); f.range.value = '0.37'; f.range.fire('input'); f.range.fire('change');
  assert.deepEqual(f.reverbs, [0.37]); assert.equal(f.readout.textContent, '37%');
  assert.equal(f.control.expanded, true); assert.equal(f.document.activeElement, f.range);
  f.control.setReverb(0); assert.equal(f.readout.textContent, 'Off'); assert.deepEqual(f.reverbs, [0.37]);
  f.control.setEffectsError('Reverb is unavailable.'); assert.equal(f.effectsStatus.hidden, false); assert.equal(f.effectsStatus.textContent, 'Reverb is unavailable.');
  f.control.setEffectsError(''); assert.equal(f.effectsStatus.hidden, true);
  assert.equal(normalizeReverb(-1), 0); assert.equal(normalizeReverb(2), 1); assert.equal(normalizeReverb('invalid'), 0);
});

test('reduced motion skips popup animation and closed controls are inert', () => {
  const f = fixture(); let animations = 0;
  f.document.defaultView.matchMedia = () => ({ matches: true });
  f.panel.animate = () => { animations++; throw new Error('Should respect reduced motion'); };
  f.control.open(); f.control.close();
  assert.equal(animations, 0); assert.equal(f.panel.hidden, true); assert.equal(f.panel.inert, true);
});

test('destroy removes document listeners so an old speed control cannot react', () => {
  const f = fixture(); f.control.open(); f.control.destroy();
  f.range.value = '1.24'; f.range.fire('input'); f.outside.fire('pointerdown');
  assert.deepEqual(f.rates, []); assert.equal(f.control.expanded, true);
  assert.equal(f.document.listeners.get('pointerup').size, 0);
});

function disclosureFixture(saved = false) {
  const document = new Node(), root = new Node(document, document), toggle = new Node(document, root), content = new Node(document, root), label = new Node(document, toggle), audio = new Node(document, content);
  audio.paused = false; let stored = saved;
  root.selectors.set('#recommendation-toggle', toggle); root.selectors.set('#recommendation-content', content); toggle.selectors.set('[data-collapse-label]', label);
  const disclosure = new RecommendationDisclosure({ root, collapsed: saved, onCollapseChange: value => { stored = value; } });
  return { root, toggle, content, label, audio, disclosure, saved: () => stored };
}

test('recommendation collapse retains playback and saves the explicit choice for restoration', () => {
  const f = disclosureFixture(); f.toggle.focus(); f.toggle.fire('click');
  assert.equal(f.content.hidden, true); assert.equal(f.toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(f.label.textContent, 'Show'); assert.equal(f.saved(), true); assert.equal(f.audio.paused, false);
  const restored = disclosureFixture(f.saved());
  assert.equal(restored.content.hidden, true); restored.toggle.fire('click');
  assert.equal(restored.content.hidden, false); assert.equal(restored.saved(), false); assert.equal(restored.audio.paused, false);
});
