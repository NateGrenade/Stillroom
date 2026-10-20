/** Browser compatibility and factual source/saved audio details. */
export function supportsNativeOpus(createAudio = () => new Audio()) {
  try { return Boolean(createAudio().canPlayType('audio/webm; codecs="opus"')); }
  catch { return false; }
}
export function storageBytes(value) {
  if (!Number.isFinite(value) || value < 0) return 'Size unavailable';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
}
export function storageDescription(value) {
  if (!value || typeof value !== 'object') return 'Details unavailable';
  const codec = typeof value.codec === 'string' && value.codec ? value.codec.toUpperCase() : 'Audio';
  const rate = Number.isFinite(value.bitrate) && value.bitrate > 0 ? ` · ${value.bitrateEstimated ? '≈' : ''}${(value.bitrate / 1000).toFixed(1)} kb/s` : '';
  return `${codec}${rate} · ${storageBytes(value.bytes)}`;
}
