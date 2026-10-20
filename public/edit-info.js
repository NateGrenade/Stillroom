/** Edit library labels without touching the audio or playback transport. */
export class EditInfoDialog {
  constructor({ dialog, api, showDialog, onSave, onMissing = () => {}, onClose = () => {}, toast }) {
    Object.assign(this, { dialog, api, showDialog, onSave, onMissing, onClose, toast });
    this.$ = selector => dialog.querySelector(selector);
    this.$$ = selector => [...dialog.querySelectorAll(selector)];
    this.trackId = null;
    this.busy = false;
    this.missing = false;
    this.$('#edit-info-form').addEventListener('submit', event => { event.preventDefault(); this.save(); });
    this.dialog.addEventListener('cancel', event => { if (this.busy) event.preventDefault(); });
    this.dialog.addEventListener('close', () => {
      this.onClose(this.trackId);
      this.trackId = null;
    });
  }
  status(message = '') {
    const node = this.$('#edit-info-status');
    node.textContent = message;
    node.hidden = !message;
  }
  render() {
    this.$$('input').forEach(node => { node.disabled = this.busy || this.missing; });
    this.$$('.close-dialog').forEach(node => { node.disabled = this.busy; });
    this.$('#edit-info-save').disabled = this.busy || this.missing;
    this.$('#edit-info-save').textContent = this.busy ? 'Saving…' : 'Save changes';
    this.$('#edit-info-form').setAttribute('aria-busy', String(this.busy));
  }
  open(track) {
    if (this.busy || !track?.id) return;
    this.trackId = track.id;
    this.missing = false;
    for (const field of ['title', 'artist', 'album']) this.$(`#edit-info-${field}`).value = track[field] || '';
    this.status();
    this.render();
    this.showDialog(this.dialog);
    this.$('#edit-info-title').focus();
    this.$('#edit-info-title').select();
  }
  async save() {
    if (this.busy || this.missing || !this.trackId) return;
    const changes = Object.fromEntries(['title', 'artist', 'album'].map(field => [field, this.$(`#edit-info-${field}`).value.trim()]));
    if (!changes.title) {
      this.status('Enter a song title.');
      this.$('#edit-info-title').focus();
      return;
    }
    const trackId = this.trackId;
    this.busy = true;
    this.status('Saving changes…');
    this.render();
    try {
      const result = await this.api(`/api/tracks/${encodeURIComponent(trackId)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes),
      });
      if (result?.track?.id !== trackId) throw new Error('The saved song could not be confirmed. Please try again.');
      this.onSave(result.track);
      this.dialog.close();
      this.toast('Song info updated.');
    } catch (error) {
      if (error.status === 404) {
        this.missing = true;
        this.status('This song was removed from your library. Close this window to continue.');
        this.onMissing();
      } else this.status(error.message || 'The song info could not be saved. Please try again.');
    } finally {
      this.busy = false;
      this.render();
    }
  }
}
