#!/usr/bin/env python3
"""
onvif_events.py - list the ONVIF event topics of a device and print every event
received through a PullPoint subscription. Standard library only.

Usage: python3 onvif_events.py --host <device-ip> [--port 2000] [--seconds 90]
"""
import argparse
import datetime
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

NS_ENV = "http://www.w3.org/2003/05/soap-envelope"
ACT_CREATE = "http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest"
ACT_PULL = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
ACT_PROPS = "http://www.onvif.org/ver10/events/wsdl/EventPortType/GetEventPropertiesRequest"
ACT_UNSUB = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"


def soap(url, action, body, verbose=False):
    env = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="%s" xmlns:wsa="http://www.w3.org/2005/08/addressing" '
           'xmlns:tev="http://www.onvif.org/ver10/events/wsdl" '
           'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">'
           '<s:Header><wsa:Action>%s</wsa:Action><wsa:To>%s</wsa:To></s:Header>'
           '<s:Body>%s</s:Body></s:Envelope>') % (NS_ENV, action, url, body)
    req = urllib.request.Request(url, data=env.encode(), method="POST", headers={
        "Content-Type": 'application/soap+xml; charset=utf-8; action="%s"' % action})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        data = e.read().decode("utf-8", "replace")
        print("HTTP %d sur %s" % (e.code, action.rsplit("/", 1)[-1]), file=sys.stderr)
    except (urllib.error.URLError, OSError) as e:
        return "ERREUR_CONNEXION %s" % e
    if verbose:
        print(data, file=sys.stderr)
    return data


def local(tag):
    return tag.rsplit("}", 1)[-1]


def topics_from_properties(xml):
    """Walk the TopicSet and return topic paths that are marked as events."""
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    tset = next((e for e in root.iter() if local(e.tag) == "TopicSet"), None)
    if tset is None:
        return out

    def walk(node, path):
        for child in node:
            p = path + [local(child.tag)]
            if any(local(c.tag) == "MessageDescription" for c in child):
                out.append("/".join(p))
            walk(child, p)
    walk(tset, [])
    return out


def print_messages(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        print("  (réponse illisible)", xml[:300])
        return 0
    n = 0
    for nm in (e for e in root.iter() if local(e.tag) == "NotificationMessage"):
        topic = next((e.text for e in nm.iter() if local(e.tag) == "Topic"), "?")
        msg = next((e for e in nm.iter() if local(e.tag) == "Message" and "UtcTime" in e.attrib), None)
        op = msg.attrib.get("PropertyOperation", "") if msg is not None else ""
        items = ["%s=%s" % (e.attrib.get("Name"), e.attrib.get("Value"))
                 for e in nm.iter() if local(e.tag) == "SimpleItem"]
        now = datetime.datetime.now().strftime("%H:%M:%S")
        print("%s  ÉVÉNEMENT  %s  %s  %s" % (now, (topic or "").strip(), op, " ".join(items)))
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=2000)
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    svc = "http://%s:%d/onvif/device_service" % (a.host, a.port)

    print("Sujets d'événements annoncés par le boîtier :")
    for t in topics_from_properties(soap(svc, ACT_PROPS, "<tev:GetEventProperties/>", a.verbose)) or ["(aucun)"]:
        print("  -", t)

    rsp = soap(svc, ACT_CREATE, "<tev:CreatePullPointSubscription>"
               "<tev:InitialTerminationTime>PT%dS</tev:InitialTerminationTime>"
               "</tev:CreatePullPointSubscription>" % (a.seconds + 60), a.verbose)
    m = re.search(r"<[^>]*Address[^>]*>\s*([^<]+?)\s*</", rsp)
    if not m:
        print("Impossible de créer l'abonnement. Réponse :\n", rsp[:1500])
        sys.exit(1)
    sub = m.group(1).replace("&amp;", "&")
    print("\nAbonnement créé : %s" % sub)
    # Some cheap firmwares announce an address that does not answer: try alternatives.
    path = re.sub(r"^https?://[^/]+", "", sub) or "/"
    candidates = [sub, "http://%s:%d%s" % (a.host, a.port, path), svc,
                  "http://%s:%d/onvif/event_service" % (a.host, a.port),
                  "http://%s:%d/onvif/Events" % (a.host, a.port)]
    chosen = None
    for c in dict.fromkeys(candidates):
        xml = soap(c, ACT_PULL, "<tev:PullMessages><tev:Timeout>PT1S</tev:Timeout>"
                   "<tev:MessageLimit>32</tev:MessageLimit></tev:PullMessages>", a.verbose)
        ok = "PullMessagesResponse" in xml
        print("  essai %-50s -> %s" % (c, "OK" if ok else re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml))[:120]))
        if ok:
            chosen = c
            print_messages(xml)
            break
    if not chosen:
        print("\nAucune adresse ne répond aux demandes d'événements : ce boîtier ne les fournit pas vraiment.")
        print("Réponse complète de la création d'abonnement :\n", rsp[:2000])
        sys.exit(1)
    sub = chosen
    print("Écoute pendant %d s : fais sonner maintenant (note l'heure).\n" % a.seconds)

    end = datetime.datetime.now() + datetime.timedelta(seconds=a.seconds)
    total = 0
    try:
        while datetime.datetime.now() < end:
            xml = soap(sub, ACT_PULL, "<tev:PullMessages><tev:Timeout>PT5S</tev:Timeout>"
                       "<tev:MessageLimit>32</tev:MessageLimit></tev:PullMessages>", a.verbose)
            if xml.startswith("ERREUR_CONNEXION") or ("Fault" in xml and "NotificationMessage" not in xml):
                print("Erreur du boîtier :", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml))[:400])
                break
            total += print_messages(xml)
    except KeyboardInterrupt:
        pass
    finally:
        soap(sub, ACT_UNSUB, "<wsnt:Unsubscribe/>")
    print("\nTerminé : %d événement(s) reçu(s)." % total)


if __name__ == "__main__":
    main()
