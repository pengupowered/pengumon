// State shared between modules. An imported binding cannot be assigned, so the values that change at run time
// live as properties of one object; module-private state stays a plain `let` in its own module.
export const state = {
  latencyChart: null,
  lossChart: null,
  currentMode: 'live', // 'live', 'custom' or seconds (900, 3600, 86400)
  lanUrl: '',
  // devices and features are filled by devices.js from /api/devices and /api/features at startup. There are no
  // device roles: a panel names its device, and a single-device view asks devices.js primaryDevice(capability).
  devices: [],
  devicesKnown: false,
  features: [],
  ispName: null, // line.isp_name from /api/info
  networkInfo: null // the page's /api/info answer (chrome.js), also announced as a `networkinfo` event on document
};

export const maxLivePoints = 60;
