export function scheduleEditorType(schedule = {}) {
  const recurrenceType = String(schedule.recurrence_type || 'daily');
  const rrule = String(schedule.rrule || '').trim().toUpperCase();
  if (recurrenceType === 'rfc5545' && rrule === 'FREQ=WEEKLY') return 'weekly';
  return recurrenceType;
}

export function recurrenceSubmissionPolicy(selectedType, { intervalSeconds, timezoneName, rrule } = {}) {
  const kind = String(selectedType || 'daily');
  if (kind === 'weekly') {
    return {
      recurrenceType: 'rfc5545',
      intervalSeconds: null,
      timezoneName: String(timezoneName || 'UTC'),
      rrule: 'FREQ=WEEKLY',
    };
  }
  return {
    recurrenceType: kind,
    intervalSeconds: kind === 'interval' ? Number(intervalSeconds || 0) : null,
    timezoneName: kind === 'interval' ? 'UTC' : String(timezoneName || 'UTC'),
    rrule: kind === 'rfc5545' ? String(rrule || '').trim() : '',
  };
}

export function zonedDateTimeLocalValue(value, timezoneName = 'UTC') {
  const instant = new Date(String(value || ''));
  if (!Number.isFinite(instant.getTime())) return '';
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: String(timezoneName || 'UTC'),
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(instant);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    if (!values.year || !values.month || !values.day || !values.hour || !values.minute) return '';
    return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`;
  } catch (_error) {
    return '';
  }
}
