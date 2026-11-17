# LoomChat web client: the same index.html the Android app ships, served by nginx.
# nginx also proxies /_matrix to the homeserver, so the page and the API share
# one origin (no CORS setup, and "blank homeserver" at login just works).
FROM nginx:1.27-alpine
COPY docker/loomchat/nginx.conf /etc/nginx/conf.d/default.conf
COPY clients/loomchat/src/assets/ /usr/share/nginx/html/
