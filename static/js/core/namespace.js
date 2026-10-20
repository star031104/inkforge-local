/* InkForge browser namespace (loaded before every other script).
   The frontend stays global-scope scripts (no bundler, no ES modules). Each
   feature additionally registers its public API once:
     Inkforge.register("director", {startFullBookDirector, ...});
   New code should call Inkforge.director.startFullBookDirector() rather than
   the bare global; the globals remain for compatibility. register() merges
   into an existing entry, so a feature may register in several steps. */
window.Inkforge = window.Inkforge || {};
if (typeof window.Inkforge.register !== "function") {
  Object.defineProperty(window.Inkforge, "register", {
    enumerable: false,
    value: function register(name, api) {
      if (!name || typeof name !== "string" || name === "register") {
        throw new TypeError("Inkforge.register(name, api): invalid name");
      }
      const current = window.Inkforge[name];
      const target = current && typeof current === "object"
        ? Object.assign(current, api || {})
        : (api && typeof api === "object" ? api : {});
      window.Inkforge[name] = target;
      return target;
    },
  });
}
