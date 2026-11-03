"""Shared fake of the few `az` calls the kit makes: account, provider, group, deployment, Graph user lookup and the ARM REST
calls for the Fabric capacity. Stateful, so lifecycle and wipe flows can be exercised end to end."""
import json
from pathlib import Path

NOT_FOUND = "(ResourceNotFound) The Resource 'Microsoft.Fabric/capacities/contosofabricf4' under resource group 'rg' was not found."


def make_cfg(**over):
    from contoso_onto.cli import DEFAULT_CONFIG
    c = json.loads(json.dumps(DEFAULT_CONFIG))
    c.update(over)
    return c


class FakeAz:
    def __init__(self, user_type="user", rg_exists=False, validate_rc=0, cap=None, others_in_rg=0, provider="Registered", fail_delete=False, rg_tags=None):
        self.calls = []
        self.user_type, self.rg_exists, self.validate_rc, self.provider = user_type, rg_exists, validate_rc, provider
        self.foreign = [{"name": f"other-{i}", "type": "Microsoft.Storage/storageAccounts"} for i in range(others_in_rg)]   # resources someone else put in the group
        self.other_caps = []                                                 # Fabric capacities of the subscription that this kit did not create (the wipe audit lists them)
        self.rg_tags = dict(rg_tags or {})
        self.cap = cap                                                       # {"sku": "F4", "state": "Active"} or None
        self.fail_delete = fail_delete
        self.bodies = []
        self.requested_skus = []                                             # the size each capacity deployment asked for

    def _cap_json(self):
        return json.dumps({"name": "contosofabricf4", "id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Fabric/capacities/contosofabricf4",
                           "sku": {"name": self.cap["sku"]}, "location": "southindia",
                           "properties": {"state": self.cap["state"], "provisioningState": "Succeeded", "administration": {"members": ["me@contoso.com"]}}})

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["account", "show"]:
            return 0, json.dumps({"id": "sub-1", "name": "Sub", "tenantId": "t-1", "user": {"name": "me@contoso.com", "type": self.user_type}}), ""
        if args[:2] == ["provider", "show"]:
            return 0, self.provider, ""
        if args[:2] == ["provider", "register"]:
            self.provider = "Registered"
            return 0, "", ""
        if args[:2] == ["group", "exists"]:
            return 0, "true" if self.rg_exists else "false", ""
        if args[:2] == ["group", "create"]:
            self.rg_exists = True
            if "--tags" in args:                                              # --tags k=v k=v ... up to the next option
                for t in args[args.index("--tags") + 1:]:
                    if t.startswith("-"):
                        break
                    k, _, v = t.partition("=")
                    self.rg_tags[k] = v
            return 0, "{}", ""
        if args[:2] == ["group", "show"]:
            if not self.rg_exists:
                return 1, "", "(ResourceGroupNotFound) Resource group 'rg' could not be found."
            return 0, json.dumps({"name": args[args.index("-n") + 1], "tags": self.rg_tags or None}), ""
        if args[:2] == ["group", "delete"]:
            self.rg_exists, self.cap, self.rg_tags = False, None, {}
            return 0, "", ""
        if args[:2] == ["resource", "list"]:
            if "--resource-type" in args:                                     # subscription-wide audit of Fabric capacities
                own = [{"name": "contosofabricf4", "resourceGroup": "rg", "location": "southindia", "sku": {"name": self.cap["sku"]}}] if self.cap else []
                return 0, json.dumps(own + self.other_caps), ""
            if not self.rg_exists:
                return 1, "", "(ResourceGroupNotFound) Resource group 'rg' could not be found."
            mine = [{"name": "contosofabricf4", "type": "Microsoft.Fabric/capacities"}] if self.cap else []
            return 0, json.dumps(mine + self.foreign), ""
        if args[:2] == ["ad", "user"]:
            return 0, "oid-" + args[args.index("--id") + 1].split("@")[0], ""
        if args[:3] == ["deployment", "group", "validate"]:
            return self.validate_rc, "", ("RequestDisallowedByPolicy: Fabric capacities are not allowed in this region" if self.validate_rc else "")
        if args[:3] == ["deployment", "group", "what-if"]:
            return 0, json.dumps({"changes": [{"changeType": "Create", "resourceId": "/subscriptions/s/resourceGroups/r/providers/Microsoft.Fabric/capacities/contosofabricf4"},
                                              {"changeType": "Ignore", "resourceId": "/subscriptions/s/resourceGroups/r/providers/Microsoft.Foo/bars/x"}]}), ""
        if args[:3] == ["deployment", "group", "create"]:
            sku = "F4"
            if "-p" in args and args[args.index("-p") + 1].startswith("@"):      # the capacity is whatever size the parameter file asked for
                sku = json.loads(Path(args[args.index("-p") + 1][1:]).read_text(encoding="utf-8"))["parameters"]["sku"]["value"]
            self.requested_skus.append(sku)
            self.cap = {"sku": sku, "state": "Active"}
            return 0, "", ""
        if args[0] == "rest":
            method, url = args[args.index("--method") + 1].lower(), args[args.index("--url") + 1]
            if "--body" in args:
                self.bodies.append(json.loads(Path(args[args.index("--body") + 1][1:]).read_text()))
            if "checkNameAvailability" in url:
                return 0, json.dumps({"nameAvailable": self.cap is None}), ""
            if "/capacities/" not in url:
                return 1, "", "unexpected url " + url
            if method == "delete" and self.fail_delete:
                return 1, "", "(Conflict) capacity is busy"
            if self.cap is None or not self.rg_exists:
                return (0, "", "") if method == "delete" else (1, "", NOT_FOUND)
            if method == "get":
                return 0, self._cap_json(), ""
            if method == "post" and "/suspend" in url:
                self.cap["state"] = "Paused"
            elif method == "post" and "/resume" in url:
                self.cap["state"] = "Active"
            elif method == "patch":
                self.cap["sku"] = self.bodies[-1]["sku"]["name"]
            elif method == "delete":
                self.cap = None
            return 0, "", ""
        return 0, "{}", ""

    def methods(self):
        return [(c[c.index("--method") + 1], c[c.index("--url") + 1].split("?")[0].rsplit("/", 1)[-1]) for c in self.calls if c[0] == "rest"]
