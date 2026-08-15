# First site

1. Sign in to the panel (`http://YOUR_HOSTNAME:8080`).
2. Create a hosting user (OS account + home jail).
3. Open **Sites** → pick user, domain, and stack.
4. Stacks that need SQL only appear if you installed MariaDB and/or PostgreSQL.
5. Point DNS **A record** for that exact hostname at the server public IP, then open `http://your-domain` (not the raw IP — Traefik matches on Host).
6. HTTPS/Let’s Encrypt issues after DNS propagates; HTTP works immediately once DNS points here.
