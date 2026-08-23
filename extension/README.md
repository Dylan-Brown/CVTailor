# CVTailor Extension Tools

This directory contains the browser-based tools required to extract job data from the web and feed it into the CVTailor pipeline. 

It is split into two components:

* **`/chrome`**: The Job Data Harvester Chrome extension. This is the actual browser tool that scrapes job descriptions and packages them into a structured JSON payload.
* **`/native_host`**: An optional (but recommended) local messaging bridge. It allows the Chrome extension to pipe data directly into your CVTailor `applications/` folder, bypassing the need to manually move files out of your Downloads folder.

**Where to start?**
Head into the [`/chrome`](./chrome) directory first to install the extension, then check out [`/native_host`](./native_host) to set up the automated data pipe.