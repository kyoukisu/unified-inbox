{ config, lib, pkgs, ... }:

let
  cfg = config.services.unified-inbox;
  compose = "${pkgs.docker-compose}/bin/docker-compose";
  reload = pkgs.writeShellScript "unified-inbox-reload" ''
    set -eu
    ${compose} up -d --build --remove-orphans
    ${pkgs.docker}/bin/docker image prune --force --filter until=24h || true
    ${pkgs.docker}/bin/docker builder prune --force --filter until=168h || true
  '';
in
{
  options.services.unified-inbox = {
    enable = lib.mkEnableOption "containerized Telegram unified inbox bridge";

    projectDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/opt/unified-inbox";
      description = "Checkout containing compose.yaml, .env, and local secret files.";
    };

    buildProxy = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Optional proxy exported to docker-compose during builds.";
    };
  };

  config = lib.mkIf cfg.enable {
    virtualisation.docker.enable = true;

    systemd.services.unified-inbox = {
      description = "Telegram unified inbox bridge";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "docker.service" "network-online.target" ];
      requires = [ "docker.service" ];
      restartIfChanged = true;

      environment = lib.mkIf (cfg.buildProxy != null) {
        HTTP_PROXY = cfg.buildProxy;
        HTTPS_PROXY = cfg.buildProxy;
        ALL_PROXY = cfg.buildProxy;
        NO_PROXY = "127.0.0.1,localhost,core,discord-adapter,steam-adapter";
      };

      unitConfig.ConditionPathExists = "${cfg.projectDirectory}/compose.yaml";

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        WorkingDirectory = cfg.projectDirectory;
        UMask = "0077";
        TimeoutStartSec = "10min";
        TimeoutStopSec = "2min";
        ExecStartPre = [
          "${pkgs.coreutils}/bin/test -s ${cfg.projectDirectory}/secrets/telegram_bot_token"
          "${pkgs.coreutils}/bin/test -s ${cfg.projectDirectory}/secrets/telegram_outbox_bot_token"
          "${pkgs.coreutils}/bin/test -s ${cfg.projectDirectory}/secrets/discord_user_token"
          "${pkgs.coreutils}/bin/test -s ${cfg.projectDirectory}/secrets/internal_api_token"
          "${compose} config --quiet"
        ];
        ExecStart = "${compose} up -d --remove-orphans";
        ExecReload = reload;
        ExecStop = "${compose} down";
      };
    };
  };
}
