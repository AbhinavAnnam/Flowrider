import {defineConfig} from 'playwright/test';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';

const python=process.env.FLOWRIDER_PYTHON || (existsSync(resolve('../.venv/bin/python'))?resolve('../.venv/bin/python'):'python3');
const quote=value=>"'"+value.replaceAll("'","'\\''")+"'";

export default defineConfig({
  testDir:'./tests',workers:1,reporter:'list',
  use:{baseURL:'http://127.0.0.1:18080',headless:true,launchOptions:{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}},
  webServer:{command:`${quote(python)} ../demo_server.py --port 18080`,
    url:'http://127.0.0.1:18080/api/health',reuseExistingServer:false},
});
