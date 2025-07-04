#
# This file is part of snmpsim software.
#
# Copyright (c) 2010-2019, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/snmpsim/license.html
#
# Managed value variation module
# Send SNMP Notification
#
import time
import asyncio
import threading
from pysnmp.hlapi.asyncio import *
from pysnmp.hlapi.v3arch.asyncio.transport import UdpTransportTarget

from snmpsim import error
from snmpsim import log
from snmpsim.grammar.snmprec import SnmprecGrammar
from snmpsim.record.snmprec import SnmprecRecord
from snmpsim.utils import run_in_new_loop, split, run_in_loop_with_return


def init(**context):
    pass


TYPE_MAP = {
    "s": OctetString,
    "h": lambda x: OctetString(hexValue=x),
    "i": Integer32,
    "o": ObjectIdentifier,
    "a": IpAddress,
    "u": Unsigned32,
    "g": Gauge32,
    "t": TimeTicks,
    "b": Bits,
    "I": Counter64,
}

MODULE_OPTIONS = (
    ("op", "set"),
    ("community", "public"),
    ("authkey", None),
    ("authproto", "md5"),
    ("privkey", None),
    ("privproto", "des"),
    ("proto", "udp"),
    ("port", "162"),
    ("ntftype", "trap"),
    ("trapoid", "1.3.6.1.6.3.1.1.5.1"),
)


def _cbFun(
    snmpEngine,
    sendRequestHandle,
    errorIndication,
    errorStatus,
    errorIndex,
    varBinds,
    cbCtx,
):
    oid, value = cbCtx

    if errorIndication or errorStatus:
        log.info(
            "notification: for %s=%r failed with errorIndication %s, "
            "errorStatus %s" % (oid, value, errorIndication, errorStatus)
        )


def variate(oid, tag, value, **context):
    if "snmpEngine" in context and context["snmpEngine"]:
        snmpEngine = context["snmpEngine"]

        if snmpEngine not in moduleContext:
            moduleContext[snmpEngine] = {}

        if context["transportDomain"] not in moduleContext[snmpEngine]:
            # register this SNMP Engine to handle our transports'
            # receiver IDs (which we build by outbound and simulator
            # transportDomains concatenation)
            snmpEngine.register_transport_dispatcher(
                snmpEngine.transport_dispatcher,
                UdpTransportTarget.TRANSPORT_DOMAIN + context["transportDomain"],
            )

            snmpEngine.register_transport_dispatcher(
                snmpEngine.transport_dispatcher,
                Udp6TransportTarget.TRANSPORT_DOMAIN + context["transportDomain"],
            )

            moduleContext[snmpEngine][context["transportDomain"]] = 1

    else:
        raise error.SnmpsimError(
            "Variation module is not given snmpEngine. "
            "Make sure you are not running in --v2c-arch mode"
        )

    if not context["nextFlag"] and not context["exactMatch"]:
        return context["origOid"], tag, context["errorStatus"]

    if "settings" not in recordContext:
        recordContext["settings"] = dict([split(x, "=") for x in split(value, ",")])

        for k, v in MODULE_OPTIONS:
            recordContext["settings"].setdefault(k, v)

        if "hexvalue" in recordContext["settings"]:
            recordContext["settings"]["value"] = [
                int(recordContext["settings"]["hexvalue"][x : x + 2], 16)
                for x in range(0, len(recordContext["settings"]["hexvalue"]), 2)
            ]

        if "vlist" in recordContext["settings"]:
            vlist = {}

            recordContext["settings"]["vlist"] = split(
                recordContext["settings"]["vlist"], ":"
            )

            while recordContext["settings"]["vlist"]:
                o, v = recordContext["settings"]["vlist"][:2]

                vlist = recordContext["settings"]["vlist"][2:]
                recordContext["settings"]["vlist"] = vlist

                typeTag, _ = SnmprecRecord.unpack_tag(tag)

                v = SnmprecGrammar.TAG_MAP[typeTag](v)

                if o not in vlist:
                    vlist[o] = set()

                if o == "eq":
                    vlist[o].add(v)

                elif o in ("lt", "gt"):
                    vlist[o] = v

                else:
                    log.info(
                        "notification: bad vlist syntax: "
                        "%s" % recordContext["settings"]["vlist"]
                    )

            recordContext["settings"]["vlist"] = vlist

    args = recordContext["settings"]

    if context["setFlag"] and "vlist" in args:
        if "eq" in args["vlist"] and context["origValue"] in args["vlist"]["eq"]:
            pass

        elif "lt" in args["vlist"] and context["origValue"] < args["vlist"]["lt"]:
            pass

        elif "gt" in args["vlist"] and context["origValue"] > args["vlist"]["gt"]:
            pass

        else:
            return oid, tag, context["origValue"]

    if args["op"] not in ("get", "set", "any", "*"):
        log.info(
            "notification: unknown SNMP request type configured: " "%s" % args["op"]
        )
        return context["origOid"], tag, context["errorStatus"]

    if (
        args["op"] == "get"
        and not context["setFlag"]
        or args["op"] == "set"
        and context["setFlag"]
        or args["op"] in ("any", "*")
    ):
        if args["version"] in ("1", "2c"):
            authData = CommunityData(
                args["community"], mpModel=args["version"] == "2c" and 1 or 0
            )

        elif args["version"] == "3":
            if args["authproto"] == "md5":
                authProtocol = usmHMACMD5AuthProtocol

            elif args["authproto"] == "sha":
                authProtocol = usmHMACSHAAuthProtocol

            elif args["authproto"] == "none":
                authProtocol = usmNoAuthProtocol

            else:
                log.info("notification: unknown auth proto " "%s" % args["authproto"])
                return context["origOid"], tag, context["errorStatus"]

            if args["privproto"] == "des":
                privProtocol = usmDESPrivProtocol

            elif args["privproto"] == "aes":
                privProtocol = usmAesCfb128Protocol

            elif args["privproto"] == "none":
                privProtocol = usmNoPrivProtocol

            else:
                log.info(
                    "notification: unknown privacy proto " "%s" % args["privproto"]
                )
                return context["origOid"], tag, context["errorStatus"]

            authData = UsmUserData(
                args["user"],
                args["authkey"],
                args["privkey"],
                authProtocol=authProtocol,
                privProtocol=privProtocol,
            )

        else:
            log.info("notification: unknown SNMP version %s" % args["version"])
            return context["origOid"], tag, context["errorStatus"]

        if "host" not in args:
            log.info(
                "notification: target hostname not configured for " "OID %s" % (oid,)
            )
            return context["origOid"], tag, context["errorStatus"]

        address = (args["host"], int(args["port"]))
        if args["proto"] == "udp":
            target = run_in_loop_with_return(UdpTransportTarget.create(address))
        elif args["proto"] == "udp6":
            target = run_in_loop_with_return(Udp6TransportTarget.create(address))
        else:
            log.info("notification: unknown transport %s" % args["proto"])
            return context["origOid"], tag, context["errorStatus"]

        localAddress = None

        if "bindaddr" in args:
            localAddress = args["bindaddr"]

        else:
            transportDomain = context["transportDomain"][: len(target.TRANSPORT_DOMAIN)]
            if transportDomain == target.TRANSPORT_DOMAIN:
                # localAddress = snmpEngine.transportDispatcher.getTransport(
                #     context["transportDomain"]
                # ).getLocalAddress()[0]
                pass

            else:
                log.info(
                    "notification: incompatible network transport types used by "
                    "CommandResponder vs NotificationOriginator"
                )

                if "bindaddr" in args:
                    localAddress = args["bindaddr"]

        if localAddress:
            log.info("notification: binding to local address %s" % localAddress)
            target.setLocalAddress((localAddress, 0))

        # this will make target objects different based on their bind address
        target.TRANSPORT_DOMAIN = target.TRANSPORT_DOMAIN + context["transportDomain"]

        varBinds = []

        if "uptime" in args:
            varBinds.append(
                (ObjectIdentifier("1.3.6.1.2.1.1.3.0"), TimeTicks(args["uptime"]))
            )

        if args["version"] == "1":
            if "agentaddress" in args:
                varBinds.append(
                    (
                        ObjectIdentifier("1.3.6.1.6.3.18.1.3.0"),
                        IpAddress(args["agentaddress"]),
                    )
                )

            if "enterprise" in args:
                varBinds.append(
                    (
                        ObjectIdentifier("1.3.6.1.6.3.1.1.4.3.0"),
                        ObjectIdentifier(args["enterprise"]),
                    )
                )

        if "varbinds" in args:
            vbs = split(args["varbinds"], ":")
            while vbs:
                varBinds.append((ObjectIdentifier(vbs[0]), TYPE_MAP[vbs[1]](vbs[2])))
                vbs = vbs[3:]

        notificationType = NotificationType(
            ObjectIdentity(args["trapoid"])
        ).add_varbinds(*varBinds)

        run_in_new_loop(
            send_notification(
                snmpEngine,
                authData,
                target,
                ContextData(),
                args["ntftype"],
                notificationType,
                cbFun=_cbFun,
                cbCtx=(oid, value),
            )
        )

        log.info(
            "notification: sending Notification to %s with credentials "
            "%s" % (authData, target)
        )

    if context["setFlag"] or "value" not in args:
        return oid, tag, context["origValue"]

    else:
        return oid, tag, args["value"]


def shutdown(**context):
    pass
