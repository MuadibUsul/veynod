// The desktop client runs the sidecar without a console. A console child
// spawned with windowsHide=false gets its own black window on Windows, which
// covers the app. OAR spawns the runtime CLI itself, so force the flag for
// every child instead of relying on each call site.
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";

if (process.platform === "win32") {
  const hidden = (options) => ({ ...(options || {}), windowsHide: true });

  const spawn = childProcess.spawn;
  childProcess.spawn = function (...args) {
    const [command, maybeArgs, maybeOptions] = args;
    if (Array.isArray(maybeArgs)) {
      return spawn.call(this, command, maybeArgs, hidden(maybeOptions));
    }
    return spawn.call(this, command, hidden(maybeArgs));
  };

  const spawnSync = childProcess.spawnSync;
  childProcess.spawnSync = function (...args) {
    const [command, maybeArgs, maybeOptions] = args;
    if (Array.isArray(maybeArgs)) {
      return spawnSync.call(this, command, maybeArgs, hidden(maybeOptions));
    }
    return spawnSync.call(this, command, hidden(maybeArgs));
  };

  const execFile = childProcess.execFile;
  childProcess.execFile = function (file, args, options, callback) {
    if (typeof args === "function") {
      callback = args;
      args = [];
      options = {};
    } else if (typeof options === "function") {
      callback = options;
      options = {};
    }
    return execFile.call(this, file, args ?? [], hidden(options), callback);
  };

  syncBuiltinESMExports();
}
