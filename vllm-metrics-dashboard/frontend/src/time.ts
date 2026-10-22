export const DEFAULT_TIMEZONE = "Asia/Hong_Kong";

export function localInput(date: Date, timeZone = DEFAULT_TIMEZONE) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`;
}

export function wallTimeToIso(wall: string, timeZone: string) {
  const [datePart, timePart] = wall.split("T");
  if (!datePart || !timePart) throw new Error("Choose a valid date and time.");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute] = timePart.split(":").map(Number);
  const target = Date.UTC(year, month - 1, day, hour, minute);
  let guess = target;
  const formatter = new Intl.DateTimeFormat("en-GB", { timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  let p: Record<string, string> = {};
  for (let n = 0; n < 4; n += 1) {
    p = Object.fromEntries(formatter.formatToParts(new Date(guess)).map((part) => [part.type, part.value]));
    const shown = Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day), Number(p.hour), Number(p.minute), Number(p.second));
    const correction = target - shown;
    guess += correction;
    if (correction === 0) break;
  }
  p = Object.fromEntries(formatter.formatToParts(new Date(guess)).map((part) => [part.type, part.value]));
  if (Number(p.year) !== year || Number(p.month) !== month || Number(p.day) !== day || Number(p.hour) !== hour || Number(p.minute) !== minute) {
    throw new Error("This wall-clock time does not exist in the selected timezone.");
  }
  return new Date(guess).toISOString();
}
