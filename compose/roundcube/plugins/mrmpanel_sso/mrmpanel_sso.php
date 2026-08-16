<?php

/**
 * One-click webmail login from the mrmpanel control panel.
 *
 * The panel writes a one-time token under /var/lib/mrmpanel/webmail-sso.
 * This plugin consumes it and authenticates to IMAP via a Dovecot master user.
 */
class mrmpanel_sso extends rcube_plugin
{
    private $sso_email = null;

    public function init()
    {
        $this->add_hook('startup', [$this, 'startup']);
        $this->add_hook('authenticate', [$this, 'authenticate']);
        $this->add_hook('storage_connect', [$this, 'storage_connect']);
        $this->add_hook('smtp_connect', [$this, 'smtp_connect']);
    }

    public function startup($args)
    {
        if ($this->request_token() === '') {
            return $args;
        }

        // A panel token always wins: drop any stale or different session so the
        // button works even when the browser still holds an old webmail login.
        if (!empty($_SESSION['user_id'])) {
            rcmail::get_instance()->kill_session();
        }

        $args['action'] = 'login';
        $args['task'] = 'login';
        return $args;
    }

    public function authenticate($args)
    {
        $token = $this->request_token();
        if ($token === '') {
            return $args;
        }

        $payload = $this->consume_token($token);
        if (!$payload) {
            $args['abort'] = true;
            $args['error'] = 'Invalid or expired webmail login link';
            return $args;
        }

        $email = strtolower(trim((string) ($payload['email'] ?? '')));
        if ($email === '' || strpos($email, '@') === false) {
            $args['abort'] = true;
            $args['error'] = 'Invalid webmail login token';
            return $args;
        }

        $this->sso_email = $email;
        $_SESSION['mrmpanel_sso_email'] = $email;

        $args['user'] = $email;
        $args['pass'] = 'mrmpanel-sso';
        $args['cookiecheck'] = false;
        $args['valid'] = true;
        return $args;
    }

    public function storage_connect($args)
    {
        $email = $_SESSION['mrmpanel_sso_email'] ?? $this->sso_email;
        if (!$email) {
            return $args;
        }

        $master_pass = $this->master_password();
        if ($master_pass === '') {
            rcube::write_log('errors', 'mrmpanel_sso: master password unreadable');
            return $args;
        }

        $args['user'] = $email . '*mrmpanel';
        $args['pass'] = $master_pass;
        return $args;
    }

    public function smtp_connect($args)
    {
        $email = $_SESSION['mrmpanel_sso_email'] ?? $this->sso_email;
        if (!$email) {
            return $args;
        }

        $master_pass = $this->master_password();
        if ($master_pass === '') {
            return $args;
        }

        $args['smtp_user'] = $email . '*mrmpanel';
        $args['smtp_pass'] = $master_pass;
        return $args;
    }

    private function request_token()
    {
        $token = isset($_GET['_sso']) ? (string) $_GET['_sso'] : '';
        if ($token === '' && isset($_POST['_sso'])) {
            $token = (string) $_POST['_sso'];
        }
        $token = preg_replace('/[^a-f0-9]/', '', strtolower($token));
        return strlen($token) >= 32 ? $token : '';
    }

    private function consume_token($token)
    {
        $path = '/var/lib/mrmpanel/webmail-sso/' . $token . '.json';
        if (!is_file($path)) {
            return null;
        }

        $raw = @file_get_contents($path);
        @unlink($path);
        if ($raw === false || $raw === '') {
            return null;
        }

        $data = json_decode($raw, true);
        if (!is_array($data)) {
            return null;
        }

        $exp = (int) ($data['exp'] ?? 0);
        if ($exp < time()) {
            return null;
        }

        return $data;
    }

    private function master_password()
    {
        $path = '/var/lib/mrmpanel/secrets/webmail_master_password';
        if (!is_readable($path)) {
            return '';
        }
        return trim((string) file_get_contents($path));
    }
}
