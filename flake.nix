{
  description = "Telegram forum bridge for Discord and Steam direct messages";

  outputs = { self }:
    {
      nixosModules.default = import ./nix/module.nix;
      nixosModules.unified-inbox = self.nixosModules.default;
    };
}
