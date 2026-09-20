#!/usr/bin/env bash
# MoonTV CCTV6 Navigation & Reverse Proxy Patch Script
set -e

CONTAINER_NAME="moontv"

echo "=== 正在为 MoonTV 注入 CCTV6 一级栏目与反向代理 ==="

# 1. 注入侧边栏与底部导航中的 CCTV6
docker exec -u 0 "$CONTAINER_NAME" node -e '
const fs = require("fs");
const vm = require("vm");

// 1. 修改 706-66bfc47f149a3d05.js
const file706 = "/app/.next/static/chunks/706-66bfc47f149a3d05.js";
if (fs.existsSync(file706)) {
  let code706 = fs.readFileSync(file706, "utf8");
  const targetMobile = `[{icon:n.Z,label:"\\u9996\\u9875",href:"/"},{icon:d.Z,label:"\\u7535\\u5f71",href:"/douban?type=movie"}`;
  const replaceMobile = `[{icon:n.Z,label:"\\u9996\\u9875",href:"/"},{icon:d.Z,label:"CCTV6",href:"/cctv6"},{icon:d.Z,label:"\\u7535\\u5f71",href:"/douban?type=movie"}`;
  const targetDesktop = `[k,C]=(0,h.useState)([{icon:d.Z,label:"\\u7535\\u5f71",href:"/douban?type=movie"}`;
  const replaceDesktop = `[k,C]=(0,h.useState)([{icon:d.Z,label:"CCTV6",href:"/cctv6"},{icon:d.Z,label:"\\u7535\\u5f71",href:"/douban?type=movie"}`;

  if (code706.includes(targetMobile) && code706.includes(targetDesktop)) {
    code706 = code706.replace(targetMobile, replaceMobile).replace(targetDesktop, replaceDesktop);
    new vm.Script(code706);
    fs.writeFileSync(file706, code706);
    console.log("Successfully patched 706 static chunk!");
  }
}

// 2. 修改 422.js
const file422 = "/app/.next/server/chunks/422.js";
if (fs.existsSync(file422)) {
  let code422 = fs.readFileSync(file422, "utf8");
  const targetMobile422 = `[{icon:d.Z,label:"\\u9996\\u9875",href:"/"},{icon:n.Z,label:"\\u7535\\u5f71",href:"/douban?type=movie"}`;
  const replaceMobile422 = `[{icon:d.Z,label:"\\u9996\\u9875",href:"/"},{icon:n.Z,label:"CCTV6",href:"/cctv6"},{icon:n.Z,label:"\\u7535\\u5f71",href:"/douban?type=movie"}`;
  const targetDesktop422 = `[k,N]=(0,u.useState)([{icon:n.Z,label:"\\u7535\\u5f71",href:"/douban?type=movie"}`;
  const replaceDesktop422 = `[k,N]=(0,u.useState)([{icon:n.Z,label:"CCTV6",href:"/cctv6"},{icon:n.Z,label:"\\u7535\\u5f71",href:"/douban?type=movie"}`;

  if (code422.includes(targetMobile422) && code422.includes(targetDesktop422)) {
    code422 = code422.replace(targetMobile422, replaceMobile422).replace(targetDesktop422, replaceDesktop422);
    new vm.Script(code422);
    fs.writeFileSync(file422, code422);
    console.log("Successfully patched 422 server chunk!");
  }
}

// 3. 修改 server.js 注入反向代理钩子
const serverFile = "/app/server.js";
if (fs.existsSync(serverFile)) {
  let serverCode = fs.readFileSync(serverFile, "utf8");
  const targetStart = "const { startServer } = require(\x27next/dist/server/lib/start-server\x27)";
  const proxyCode = `
const http = require("http");
const originalCreateServer = http.createServer;
http.createServer = function(requestListener) {
  return originalCreateServer.call(this, (req, res) => {
    if (req.url && (req.url === "/cctv6" || req.url === "/cctv6/" || req.url.startsWith("/api/cctv6") || req.url.startsWith("/cctv6?"))) {
      const targetPath = req.url === "/cctv6/" ? "/cctv6" : req.url;
      const targetUrl = "http://192.168.0.120:5888" + targetPath;
      const proxyReq = http.request(targetUrl, {
        method: req.method,
        headers: { ...req.headers, host: "192.168.0.120:5888" }
      }, (proxyRes) => {
        res.writeHead(proxyRes.statusCode, proxyRes.headers);
        proxyRes.pipe(res, { end: true });
      });
      proxyReq.on("error", (err) => {
        res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
        res.end("CCTV-6 服务暂时不可用: " + err.message);
      });
      req.pipe(proxyReq, { end: true });
      return;
    }
    return requestListener(req, res);
  });
};
const { startServer } = require(\x27next/dist/server/lib/start-server\x27);
`;
  if (serverCode.includes(targetStart)) {
    serverCode = serverCode.replace(targetStart, proxyCode);
    new vm.Script(serverCode);
    fs.writeFileSync(serverFile, serverCode);
    console.log("Successfully patched server.js with proxy hook!");
  }
}
'

echo "=== 重启 MoonTV 容器 ==="
docker restart "$CONTAINER_NAME"
echo "=== 补丁注入完成，CCTV6 已上线到 MoonTV 左侧导航栏 ==="
