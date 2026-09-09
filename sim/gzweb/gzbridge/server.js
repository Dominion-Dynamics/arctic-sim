#!/usr/bin/env node

"use strict"

const WebSocketServer = require('websocket').server;
const http = require('http');
const fs = require('fs');
const path = require('path');
const gzbridge = require('./build/Debug/gzbridge');

/**
 * Path from where the static site is served
 */
const staticBasePath = './../http/client';

/**
 * Port to serve from, defaults to 8080
 */
const port = process.argv[2] || 8080;

/**
 * Array of websocket connections currently active, if it is empty, there are no
 * clients connected.
 */
let connections = [];

/**
 * Holds the message containing all material scripts in case there is no
 * gzserver connected
 */
let materialScriptsMessage = {};

/**
 * Whether currently connected to a gzserver
 */
let isConnected = false;

/**
 * Content types by extension. Without one the browser sniffs, and a response
 * with no type and no validators is not a candidate for the HTTP cache at all.
 */
const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js':   'application/javascript; charset=utf-8',
  '.css':  'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif':  'image/gif',
  '.svg':  'image/svg+xml',
  '.ico':  'image/x-icon',
  '.dae':  'model/vnd.collada+xml',
  '.stl':  'model/stl',
  '.obj':  'text/plain; charset=utf-8',
  '.mtl':  'text/plain; charset=utf-8',
  '.woff': 'font/woff',
  '.woff2':'font/woff2',
  '.ttf':  'font/ttf'
};

/**
 * How long a client may reuse an asset without asking. 0 (the default) means
 * every request is revalidated, which still costs a round trip but no body —
 * the assets under http/client/assets are regenerated whenever the terrain is
 * rebuilt, so serving them from cache unconditionally would show stale imagery
 * after `make terrain`. Raise it (seconds) on a link where the round trips
 * themselves hurt more than a stale texture does.
 */
const assetMaxAge = parseInt(process.env.GZWEB_ASSET_MAX_AGE || '0', 10) || 0;

/**
 * Callback to serve static files
 *
 * Every response carries validators (ETag + Last-Modified) and an explicit
 * Cache-Control, and a matching conditional request is answered 304 with no
 * body. Without them the browser re-downloaded the whole scene on every reload:
 * ~71 MB for fort_ross, of which albedo.png alone is 26 MB and iris.dae 21 MB.
 *
 * index.html and gz3d.gui.js are rewritten by the entrypoint at container
 * start (fog density, control panel), so nothing here may be cached without
 * revalidating — the ETag is derived from mtime and size, which both move when
 * the entrypoint edits them.
 *
 * @param req Request
 * @param res Response
 */
let staticServe = function(req, res) {

  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Request-Method', '*');
  res.setHeader('Access-Control-Allow-Methods', 'OPTIONS, GET, POST, PUT, DELETE');
  res.setHeader('Access-Control-Allow-Headers', '*');

  const root = path.resolve(staticBasePath);

  // Strip the query string and decode before touching the filesystem: gzweb's
  // own asset URLs are clean, but a percent-encoded path would otherwise 404.
  let urlPath;
  try {
    urlPath = decodeURIComponent(req.url.split('?')[0].split('#')[0]);
  } catch (e) {
    res.writeHead(400, 'Bad Request');
    return res.end('400: Bad Request');
  }

  if (urlPath === '/')
    urlPath = '/index.html';

  const fileLoc = path.join(root, urlPath);

  // Containment check. path.join already normalises away `..`, but only
  // comparing the result to the root proves the request cannot escape it —
  // this server is reachable from wherever SIM_BIND points.
  if (fileLoc !== root && fileLoc.indexOf(root + path.sep) !== 0) {
    res.writeHead(403, 'Forbidden');
    return res.end('403: Forbidden');
  }

  fs.stat(fileLoc, function(err, stat) {
    if (err || !stat.isFile()) {
      res.writeHead(404, 'Not Found');
      return res.end('404: File Not Found!');
    }

    // Weak-free validator: size and mtime together change on any regeneration
    // of an asset, including the entrypoint's in-place edits to the bundle.
    const etag = '"' + stat.size.toString(16) + '-' +
        stat.mtime.getTime().toString(16) + '"';
    const lastModified = stat.mtime.toUTCString();
    const isAsset = urlPath.indexOf('/assets/') === 0;

    res.setHeader('ETag', etag);
    res.setHeader('Last-Modified', lastModified);
    res.setHeader('Cache-Control', isAsset ?
        'public, max-age=' + assetMaxAge + ', must-revalidate' : 'no-cache');

    const ext = path.extname(fileLoc).toLowerCase();
    if (CONTENT_TYPES[ext])
      res.setHeader('Content-Type', CONTENT_TYPES[ext]);

    // 304 when the client already holds this exact file. ETag wins over the
    // date when both are sent, per RFC 7232.
    const inm = req.headers['if-none-match'];
    const ims = req.headers['if-modified-since'];
    const fresh = inm ? inm.split(',').some(t => t.trim() === etag)
        : (ims ? Math.floor(stat.mtime.getTime() / 1000) <=
                 Math.floor(Date.parse(ims) / 1000) : false);

    if (fresh) {
      res.writeHead(304);
      return res.end();
    }

    res.setHeader('Content-Length', stat.size);
    res.statusCode = 200;

    // Streamed, not fs.readFile: a 26 MB albedo.png was previously buffered
    // whole into the heap for every request, and two clients loading at once
    // doubled that.
    const stream = fs.createReadStream(fileLoc);
    stream.on('error', function() {
      res.destroy();
    });
    stream.pipe(res);
  });
};

// HTTP server
let httpServer = http.createServer(staticServe);
httpServer.listen(port);

console.log(new Date() + " Static server listening on port: " + port);

// Websocket
let gzNode = new gzbridge.GZNode();
if (gzNode.getIsGzServerConnected())
{
  gzNode.loadMaterialScripts(staticBasePath + '/assets');
  gzNode.setPoseMsgFilterMinimumAge(0.02);
  gzNode.setPoseMsgFilterMinimumDistanceSquared(0.00001);
  gzNode.setPoseMsgFilterMinimumQuaternionSquared(0.00001);

  console.log('--------------------------------------------------------------');
  console.log('Gazebo transport node connected to gzserver.');
  console.log('Pose message filter parameters between successive messages: ');
  console.log('  minimum seconds: ' +
      gzNode.getPoseMsgFilterMinimumAge());
  console.log('  minimum XYZ distance squared: ' +
      gzNode.getPoseMsgFilterMinimumDistanceSquared());
  console.log('  minimum Quartenion distance squared:'
      + ' ' + gzNode.getPoseMsgFilterMinimumQuaternionSquared());
  console.log('--------------------------------------------------------------');
}
else
{
  materialScriptsMessage =
      gzNode.getMaterialScriptsMessage(staticBasePath + '/assets');
}

// Start websocket server
let wsServer = new WebSocketServer({
  httpServer: httpServer,
  // You should not use autoAcceptConnections for production
  // applications, as it defeats all standard cross-origin protection
  // facilities built into the protocol and the browser.  You should
  // *always* verify the connection's origin and decide whether or not
  // to accept it.
  autoAcceptConnections: false
});

wsServer.on('request', function(request) {

  // Accept request
  let connection = request.accept(null, request.origin);

  // If gzserver is not connected just send material scripts and status
  if (!gzNode.getIsGzServerConnected())
  {
    // create error status and send it
    let statusMessage =
        '{"op":"publish","topic":"~/status","msg":{"status":"error"}}';
    connection.sendUTF(statusMessage);
    // send material scripts message
    connection.sendUTF(materialScriptsMessage);
    return;
  }

  connections.push(connection);

  if (!isConnected)
  {
    isConnected = true;
    gzNode.setConnected(isConnected);
  }

  console.log(new Date() + ' New connection accepted from: ' + request.origin +
      ' ' + connection.remoteAddress);

  // Handle messages received from client
  connection.on('message', function(message) {
    if (message.type === 'utf8') {
      console.log(new Date() + ' Received Message: ' + message.utf8Data +
          ' from ' + request.origin + ' ' + connection.remoteAddress);
      gzNode.request(message.utf8Data);
    }
    else if (message.type === 'binary') {
      console.log(new Date() + ' Received Binary Message of ' +
          message.binaryData.length + ' bytes from ' + request.origin + ' ' +
          connection.remoteAddress);
      connection.sendBytes(message.binaryData);
    }
  });

  // Handle client disconnection
  connection.on('close', function(reasonCode, description) {
    console.log(new Date() + ' Peer ' + request.origin + ' ' +
        connection.remoteAddress + ' disconnected.');

    // remove connection from array
    let conIndex = connections.indexOf(connection);
    connections.splice(conIndex, 1);

    // if there is no connection notify server that there is no connected client
    if (connections.length === 0) {
      isConnected = false;
      gzNode.setConnected(isConnected);
    }
  });
});

// If not connected, periodically send messages
if (gzNode.getIsGzServerConnected())
{
  setInterval(update, 10);

  function update()
  {
    if (connections.length > 0)
    {
      let msgs = gzNode.getMessages();
      for (let i = 0; i < connections.length; ++i)
      {
        for (let j = 0; j < msgs.length; ++j)
        {
          connections[i].sendUTF(msgs[j]);
        }
      }
    }
  }
}
