export function normalizeSteamPresence(personaState) {
  if (personaState === 0 || personaState === 7) return "offline";
  if (personaState === 1 || personaState === 5 || personaState === 6) return "online";
  if (personaState === 2) return "busy";
  if (personaState === 3 || personaState === 4) return "idle";
  return null;
}
