const SPEED_PRESETS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
const SPEED_SNAP_DISTANCE = 0.018;

export function normalizePlaybackRate(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(2, Math.max(0.5, Math.round(number * 100) / 100)) : 1;
}

export function formatPlaybackRate(value) {
  return `${normalizePlaybackRate(value).toFixed(2).replace(/\.?0+$/, '')}×`;
}

export function snapPlaybackRate(value) {
  const rate = normalizePlaybackRate(value);
  return SPEED_PRESETS.find(preset => Math.abs(rate - preset) <= SPEED_SNAP_DISTANCE) ?? rate;
}

export function normalizeReverb(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(1, Math.max(0, number)) : 0;
}

/* A small, non-modal disclosure: native Tab order remains available throughout. */
class ControlPopover {
  constructor({ root, trigger, panel, initialFocus }) {
    this.root = root;
    this.trigger = trigger;
    this.panel = panel;
    this.initialFocus = initialFocus;
    this.document = root.ownerDocument;
    this.expanded = false;
    this.listeners = [];
    this.listen(trigger, 'click', () => this.expanded ? this.close() : this.open());
    this.listen(this.document, 'pointerdown', event => {
      if (this.expanded && !root.contains(event.target) && !this.dragging) this.close({ restoreFocus: false });
    });
    this.listen(root, 'keydown', event => {
      if (!this.expanded || !['Escape', 'Enter'].includes(event.key)) return;
      if (event.key === 'Enter' && event.target === trigger) return;
      event.preventDefault();
      event.stopPropagation();
      this.close();
    });
    this.listen(root, 'focusout', event => {
      if (this.expanded && !this.dragging && !root.contains(event.relatedTarget)) this.close({ restoreFocus: false });
    });
    trigger.setAttribute('aria-expanded', 'false');
    panel.hidden = true;
    panel.inert = true;
  }

  listen(target, type, listener) {
    target.addEventListener(type, listener);
    this.listeners.push(() => target.removeEventListener(type, listener));
  }

  animate(open) {
    this.animation?.cancel();
    const reduced = this.document.defaultView?.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (!this.panel.animate || reduced) return null;
    const animation = this.panel.animate(open ? [
      { opacity: 0, transform: 'translateY(4px) scale(.97)' },
      { opacity: 1, transform: 'translateY(0) scale(1)' },
    ] : [
      { opacity: 1, transform: 'translateY(0) scale(1)' },
      { opacity: 0, transform: 'translateY(4px) scale(.97)' },
    ], { duration: open ? 140 : 110, easing: 'ease-out' });
    // Rapid reopen/close cancels an animation; that is an expected interaction.
    animation.finished.catch(() => {});
    return animation;
  }

  open() {
    if (this.expanded) return;
    this.expanded = true;
    this.panel.hidden = false;
    this.panel.inert = false;
    this.trigger.setAttribute('aria-expanded', 'true');
    this.root.classList.add('is-expanded');
    this.animation = this.animate(true);
    this.initialFocus?.focus({ preventScroll: true });
  }

  close({ restoreFocus = true } = {}) {
    if (!this.expanded) return;
    const hadFocus = this.root.contains(this.document.activeElement);
    this.expanded = false;
    this.dragging = false;
    this.panel.inert = true;
    this.trigger.setAttribute('aria-expanded', 'false');
    this.root.classList.remove('is-expanded');
    this.animation = this.animate(false);
    const finish = () => { if (!this.expanded) this.panel.hidden = true; };
    if (this.animation) this.animation.finished.then(finish, finish);
    else finish();
    if (restoreFocus && hadFocus) this.trigger.focus({ preventScroll: true });
  }

  destroy() {
    this.animation?.cancel();
    this.listeners.forEach(remove => remove());
    this.listeners = [];
  }
}

export class SpeedControl extends ControlPopover {
  constructor({ root, playbackRate = 1, pitchChanges = true, onPlaybackRateChange = () => {}, onPitchChangesChange = () => {} }) {
    const range = root.querySelector('[data-speed-range]');
    super({ root, trigger: root.querySelector('[data-speed-toggle]'), panel: root.querySelector('[data-speed-panel]'), initialFocus: range });
    this.range = range;
    this.pitch = root.querySelector('[data-speed-pitch]');
    this.readouts = [...root.querySelectorAll('[data-speed-readout]')];
    this.onPlaybackRateChange = onPlaybackRateChange;
    this.onPitchChangesChange = onPitchChangesChange;
    this.listen(range, 'pointerdown', event => {
      if (event.button !== undefined && event.button !== 0) return;
      this.dragging = true;
      this.pointerId = event.pointerId;
    });
    this.listen(range, 'keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(event.key)) {
        this.dragging = false;
        this.pointerId = undefined;
      }
    });
    this.listen(range, 'input', () => {
      // Pointer position is absolute, so the thumb leaves a snap as soon as the
      // pointer moves beyond it. Keyboard steps always retain native precision.
      const rate = this.dragging ? snapPlaybackRate(range.value) : normalizePlaybackRate(range.value);
      this.setPlaybackSettings({ playbackRate: rate });
      this.onPlaybackRateChange(rate);
    });
    this.listen(this.document, 'pointerup', event => {
      if (!this.dragging || (this.pointerId !== undefined && event.pointerId !== this.pointerId)) return;
      this.dragging = false;
      this.close();
    });
    this.listen(this.document, 'pointercancel', event => {
      if (this.pointerId !== undefined && event.pointerId !== this.pointerId) return;
      this.dragging = false;
    });
    this.listen(this.pitch, 'change', () => this.onPitchChangesChange(this.pitch.checked));
    this.setPlaybackSettings({ playbackRate, pitchChanges });
  }

  setPlaybackSettings({ playbackRate, pitchChanges } = {}) {
    if (playbackRate !== undefined) {
      const rate = normalizePlaybackRate(playbackRate);
      this.range.value = String(rate);
      const label = formatPlaybackRate(rate);
      this.readouts.forEach(node => { node.textContent = label; });
      this.range.setAttribute('aria-valuetext', `${label} speed`);
      this.range.style.setProperty('--range-progress', `${(rate - 0.5) / 1.5 * 100}%`);
      this.trigger.setAttribute('aria-label', `Playback speed ${label}. Adjust speed`);
    }
    if (pitchChanges !== undefined) this.pitch.checked = Boolean(pitchChanges);
  }
}

export class AdvancedPlaybackOptions extends ControlPopover {
  constructor({ root, reverb = 0, onReverbChange = () => {} }) {
    const range = root.querySelector('[data-reverb-range]');
    super({ root, trigger: root.querySelector('[data-advanced-toggle]'), panel: root.querySelector('[data-advanced-panel]'), initialFocus: range });
    this.range = range;
    this.readout = root.querySelector('[data-reverb-readout]');
    this.effectsStatus = root.querySelector('[data-effects-status]');
    this.listen(range, 'input', () => {
      const value = normalizeReverb(range.value);
      this.setReverb(value);
      onReverbChange(value);
    });
    this.setReverb(reverb);
  }

  setReverb(value) {
    const reverb = normalizeReverb(value);
    const label = reverb === 0 ? 'Off' : `${Math.round(reverb * 100)}%`;
    this.range.value = String(reverb);
    this.readout.textContent = label;
    this.range.setAttribute('aria-valuetext', label);
    this.range.style.setProperty('--range-progress', `${reverb * 100}%`);
  }

  setEffectsError(message) {
    if (!this.effectsStatus) return;
    this.effectsStatus.textContent = message || '';
    this.effectsStatus.hidden = !message;
  }
}

export class RecommendationDisclosure {
  constructor({ root, collapsed = false, onCollapseChange = () => {} }) {
    this.root = root;
    this.toggle = root.querySelector('#recommendation-toggle');
    this.content = root.querySelector('#recommendation-content');
    this.onCollapseChange = onCollapseChange;
    this.onClick = () => {
      this.setCollapsed(!this.collapsed);
      onCollapseChange(this.collapsed);
    };
    this.toggle.addEventListener('click', this.onClick);
    this.setCollapsed(collapsed);
  }

  setCollapsed(collapsed) {
    this.collapsed = Boolean(collapsed);
    if (this.collapsed && this.content.contains(this.root.ownerDocument.activeElement)) this.toggle.focus({ preventScroll: true });
    this.toggle.setAttribute('aria-expanded', String(!this.collapsed));
    this.toggle.setAttribute('aria-label', `${this.collapsed ? 'Show' : 'Hide'} recommendations`);
    this.toggle.querySelector('[data-collapse-label]').textContent = this.collapsed ? 'Show' : 'Hide';
    this.content.hidden = this.collapsed;
    this.root.classList.toggle('is-collapsed', this.collapsed);
  }

  destroy() { this.toggle.removeEventListener('click', this.onClick); }
}
