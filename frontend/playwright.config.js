import {defineConfig} from 'playwright/test';
import {homedir} from 'node:os';

export default defineConfig({
  testDir:'./tests',workers:1,reporter:'list',
  use:{baseURL:'http://127.0.0.1:18080',headless:true,launchOptions:{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE}},
  webServer:{command:`${process.env.FLOWRIDER_PYTHON || homedir()+'/.pyenv/versions/3.10.16/bin/python'} ../demo_server.py --port 18080`,
    url:'http://127.0.0.1:18080/api/health',reuseExistingServer:false},
});
