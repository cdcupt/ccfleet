(() => {
  "use strict";

  const STORAGE_KEY = "afterHours2027.lineup";
  const PDT_OFFSET_MINUTES = 7 * 60; // San Francisco in June 2027 is PDT (UTC-7)

  const DAYS = [
    {
      id: "fri",
      order: 0,
      tabId: "tab-fri",
      shortLabel: "Fri · Jun 11",
      fullLabel: "Friday, June 11, 2027",
      theme: "Opening Night",
      year: 2027,
      month: 6,
      dayOfMonth: 11,
    },
    {
      id: "sat",
      order: 1,
      tabId: "tab-sat",
      shortLabel: "Sat · Jun 12",
      fullLabel: "Saturday, June 12, 2027",
      theme: "Late Edition",
      year: 2027,
      month: 6,
      dayOfMonth: 12,
    },
    {
      id: "sun",
      order: 2,
      tabId: "tab-sun",
      shortLabel: "Sun · Jun 13",
      fullLabel: "Sunday, June 13, 2027",
      theme: "Closing Sessions",
      year: 2027,
      month: 6,
      dayOfMonth: 13,
    },
  ];

  const ROOMS = [
    {
      id: "the-cellar",
      name: "The Cellar",
      neighborhood: "North Beach",
      address: "418 Columbus Ave, North Beach",
      capacity: "110 seated",
      acoustics: "Unamplified brick vault",
      description:
        "Low-ceiling red-brick listening room beneath Columbus Avenue. Built for natural horn projection, upright bass resonance, and close-quarters acoustic sets.",
    },
    {
      id: "blue-room",
      name: "Blue Room",
      neighborhood: "Fillmore",
      address: "1620 Fillmore St, Fillmore",
      capacity: "180 capacity",
      acoustics: "Cedar lounge · Steinway B",
      description:
        "Mid-century cedar-lined club honoring the Fillmore's jazz lineage, anchored by a restored 1964 Steinway Model B and warm ribbon microphones.",
    },
    {
      id: "foundry-loft",
      name: "Foundry Loft",
      neighborhood: "Dogpatch",
      address: "2145 Third St, Dogpatch",
      capacity: "260 capacity",
      acoustics: "Timber hall · Full horn rig",
      description:
        "High-truss industrial loft along the Third Street corridor, tuned for brass-heavy septets, contemporary big bands, and electric late-night sessions.",
    },
  ];

  const SETS = [
    // Friday, June 11, 2027
    {
      id: "fri-1",
      day: "fri",
      venueId: "the-cellar",
      timeLabel: "19:30 – 20:45",
      startMin: 19 * 60 + 30,
      endMin: 20 * 60 + 45,
      doors: "19:00",
      late: false,
      artist: "Nadia Okafor Trio",
      style: "Hard Bop · Acoustic Trio",
      summary:
        "Tenor saxophonist Nadia Okafor opens the festival with Fog Line, a raw chordless suite written for North Beach rain.",
      description:
        "Stripping away piano and guitar, Nadia Okafor stretches melodically across open upright-bass ostinatos and crisp cymbal brushwork. Expect premieres from her upcoming Fog Line suite alongside muscular re-readings of late-1950s bop standards.",
      personnel: [
        "Nadia Okafor — tenor saxophone",
        "Devon Reyes — upright bass",
        "Kenji Takahashi — drums",
      ],
      roomNote: "Intimate seated set. Unamplified saxophone and upright bass.",
    },
    {
      id: "fri-2",
      day: "fri",
      venueId: "blue-room",
      timeLabel: "20:30 – 21:45",
      startMin: 20 * 60 + 30,
      endMin: 21 * 60 + 45,
      doors: "20:00",
      late: false,
      artist: "Tomasz Kowalski Quartet",
      style: "Chamber Jazz",
      summary:
        "Lyrical felt-piano sketches and warm flugelhorn lines bridging Warsaw conservatory harmony with West Coast cool.",
      description:
        "Pianist Tomasz Kowalski brings his long-running quartet to the Blue Room Steinway for a set of patient, cinematic compositions. Flugelhornist Simone Laurent floats over hushed odd-meter grooves that unfold without rushing.",
      personnel: [
        "Tomasz Kowalski — grand piano",
        "Simone Laurent — flugelhorn",
        "Eli Vance — double bass",
        "Mara Solis — drums",
      ],
      roomNote: "Seated cabaret tables; quiet listening policy in effect.",
    },
    {
      id: "fri-3",
      day: "fri",
      venueId: "foundry-loft",
      timeLabel: "21:45 – 23:00",
      startMin: 21 * 60 + 45,
      endMin: 23 * 60,
      doors: "21:15",
      late: false,
      artist: "The Sutter Street Septet",
      style: "Post-Bop · Four-Horn Ensemble",
      summary:
        "Trombonist Clyde Henderson leads a driving four-horn frontline steeped in 1960s San Francisco ballroom energy.",
      description:
        "Tight shout choruses, trading fours, and swaggering blues-inflected arrangements fill Foundry Loft's timber rafters. The Sutter Street Septet turns compact studio charts into sprawling live improvisations.",
      personnel: [
        "Clyde Henderson — trombone, arrangements",
        "Marcus Reid — trumpet",
        "Nora Chen — alto saxophone",
        "Julian Park — baritone saxophone",
        "Bennie Flores — piano",
        "Tasha Green — bass",
        "Omar Malik — drums",
      ],
      roomNote: "Standing room and perimeter benches; full brass reinforcement.",
    },
    {
      id: "fri-4",
      day: "fri",
      venueId: "the-cellar",
      timeLabel: "23:15 – 00:45",
      startMin: 23 * 60 + 15,
      endMin: 24 * 60 + 45,
      doors: "22:50",
      late: true,
      artist: "Soraia Mendes & The Midnight Unit",
      style: "Spiritual Jazz · Late Set",
      summary:
        "Alto flute, oud, and deep modal basslines stretching past midnight inside North Beach's brick vault.",
      description:
        "Built specifically for the small hours, Lisbon-born flutist Soraia Mendes weaves North African string timbres and hypnotic percussion into long-form modal meditations that reward unhurried listening.",
      personnel: [
        "Soraia Mendes — alto flute, voice",
        "Tariq Al-Mansoor — oud, nylon guitar",
        "Idris Cole — upright bass",
        "June Park — frame drums, percussion",
      ],
      roomNote: "Late-night candlelit seating; no entry mid-piece.",
    },

    // Saturday, June 12, 2027
    {
      id: "sat-1",
      day: "sat",
      venueId: "blue-room",
      timeLabel: "19:00 – 20:15",
      startMin: 19 * 60,
      endMin: 20 * 60 + 15,
      doors: "18:30",
      late: false,
      artist: "Hanae Sato Duo",
      style: "Improvised Chamber · Vibes & Cello",
      summary:
        "Shimmering vibraphone overtones and bowed cello counterpoint in an intimate Fillmore twilight performance.",
      description:
        "Mallet specialist Hanae Sato and cellist Mateo Alvarez converse in micro-dynamics, wooden resonance, and sudden rhythmic pivots. Their duo repertoire balances written etudes with completely improvised interludes.",
      personnel: [
        "Hanae Sato — vibraphone, marimba",
        "Mateo Alvarez — cello",
      ],
      roomNote: "Acoustic showcase with minimal overhead ribbon capture.",
    },
    {
      id: "sat-2",
      day: "sat",
      venueId: "the-cellar",
      timeLabel: "20:30 – 21:45",
      startMin: 20 * 60 + 30,
      endMin: 21 * 60 + 45,
      doors: "20:00",
      late: false,
      artist: "Miles & Mercer Organ Group",
      style: "Soul Jazz · Hammond B-3",
      summary:
        "Warm Leslie-cabinet organ, husky baritone sax, and razor-sharp pocket grooves built for a packed basement.",
      description:
        "Ray Mercer kicks relentless bass pedals on a vintage Hammond B-3 while Quentin Miles anchors the melodies on baritone saxophone. Expect smoky blues, church-rooted gospel cadences, and effortless swing.",
      personnel: [
        "Ray Mercer — Hammond B-3 organ",
        "Quentin Miles — baritone saxophone",
        "Lena Brooks — hollow-body guitar",
        "Otis Grant — drums",
      ],
      roomNote: "First-come seating; B-3 positioned center-floor.",
    },
    {
      id: "sat-3",
      day: "sat",
      venueId: "foundry-loft",
      timeLabel: "21:30 – 22:50",
      startMin: 21 * 60 + 30,
      endMin: 22 * 60 + 50,
      doors: "21:00",
      late: false,
      artist: "Kofi Boateng Electric Ensemble",
      style: "Afro-Jazz · Analog Fusion",
      summary:
        "High-voltage West African polyrhythms colliding with muted trumpet, kora, and dub-drenched Rhodes.",
      description:
        "Accra- and Oakland-based trumpeter Kofi Boateng steers a five-piece electric band through interlocking 6/8 grooves, tape-delayed brass refrains, and trance-like improvisational peaks.",
      personnel: [
        "Kofi Boateng — trumpet, analog synth",
        "Amara Diallo — kora, electric guitar",
        "Samira Haddad — electric bass",
        "Theo Jenkins — drum kit",
        "Yemi Adeyemi — talking drum, congas",
      ],
      roomNote: "Open floor standing & side seating; elevated sound stage.",
    },
    {
      id: "sat-4",
      day: "sat",
      venueId: "blue-room",
      timeLabel: "23:15 – 01:00",
      startMin: 23 * 60 + 15,
      endMin: 25 * 60,
      doors: "22:50",
      late: true,
      artist: "Celine Moreau Quartet",
      style: "Nocturne · Modern Mainstream",
      summary:
        "Velvet-toned ballads and angular late-night swing extending past midnight on Fillmore Street.",
      description:
        "Montreal saxophonist Celine Moreau closes Saturday with a masterclass in tone and pacing—moving seamlessly from breathy tenor ballads to sharp, darting soprano exchanges with pianist Arthur Bell.",
      personnel: [
        "Celine Moreau — tenor & soprano saxophones",
        "Arthur Bell — piano",
        "Noah Lindqvist — upright bass",
        "Carmen Vega — drums",
      ],
      roomNote: "Extended late-night set with a brief 10-minute pause at midnight.",
    },

    // Sunday, June 13, 2027
    {
      id: "sun-1",
      day: "sun",
      venueId: "the-cellar",
      timeLabel: "18:30 – 19:45",
      startMin: 18 * 60 + 30,
      endMin: 19 * 60 + 45,
      doors: "18:00",
      late: false,
      artist: "The Embarcadero Strings & Brass",
      style: "Third Stream · Acoustic Chamber",
      summary:
        "Muted cornet, bass clarinet, and string trio sketching atmospheric coastal miniatures for Sunday dusk.",
      description:
        "Composer Ingrid Solberg pairs three Bay Area chamber string players with jazz reed and brass improvisers, crafting translucent textures inspired by San Francisco bay fog and ship horns.",
      personnel: [
        "Ingrid Solberg — bass clarinet, conductor",
        "Gideon Shaw — cornet",
        "Clara Yoon — violin",
        "Marek Novak — viola",
        "Sonia Patel — cello",
      ],
      roomNote: "All-seated dusk performance; doors close promptly at 18:35.",
    },
    {
      id: "sun-2",
      day: "sun",
      venueId: "blue-room",
      timeLabel: "19:45 – 21:00",
      startMin: 19 * 60 + 45,
      endMin: 21 * 60,
      doors: "19:15",
      late: false,
      artist: "Darius Sterling Quintet",
      style: "Modal & Bebop",
      summary:
        "High-wire trumpet phrasing and blistering alto-sax interplay rooted in classic two-horn quintet craft.",
      description:
        "Darius Sterling and Mei-Ling Zhou trade razor-sharp unisons and fearless double-time choruses over an elastic rhythm section, honoring classic Blue Note fire while pushing into modern harmonic territory.",
      personnel: [
        "Darius Sterling — trumpet",
        "Mei-Ling Zhou — alto saxophone",
        "Victor Cruz — piano",
        "Gabe Rossi — upright bass",
        "Andre Powell — drums",
      ],
      roomNote: "Seated tables and rail viewing along the cedar bar.",
    },
    {
      id: "sun-3",
      day: "sun",
      venueId: "foundry-loft",
      timeLabel: "21:00 – 22:15",
      startMin: 21 * 60,
      endMin: 22 * 60 + 15,
      doors: "20:30",
      late: false,
      artist: "Paloma Cruz Big Band",
      style: "Contemporary Big Band",
      summary:
        "Fourteen-piece Bay Area orchestra delivering wide-screen Latin-jazz brass voicings and bold solo features.",
      description:
        "Arranger and bandleader Paloma Cruz marshals five reeds, four trumpets, three trombones, and a crackling Afro-Cuban rhythm section for Foundry Loft's grand Sunday centerpiece.",
      personnel: [
        "Paloma Cruz — bandleader, piano, arrangements",
        "14-piece Bay Area Brass, Reeds & Rhythm Orchestra",
      ],
      roomNote: "Full-loft orchestration; seated risers in rear.",
    },
    {
      id: "sun-4",
      day: "sun",
      venueId: "the-cellar",
      timeLabel: "22:30 – 00:00",
      startMin: 22 * 60 + 30,
      endMin: 24 * 60,
      doors: "22:05",
      late: true,
      artist: "After Hours Closing Collective",
      style: "All-Star Acoustic Finale",
      summary:
        "Rotating festival musicians from all three nights meet in The Cellar for an unscripted, open-tape finale.",
      description:
        "No printed setlist, no rehearsal—just musicians from across the weekend stepping in on standards, blues, and spontaneous ballads until the final chord echoes out onto Columbus Avenue.",
      personnel: [
        "Rotating ensemble of After Hours 2027 artists",
        "Hosted by Nadia Okafor & Darius Sterling",
      ],
      roomNote: "Closing celebration set; recorded live to quarter-inch tape.",
    },
  ];

  // Lookup maps
  const DAYS_BY_ID = new Map(DAYS.map((d) => [d.id, d]));
  const ROOMS_BY_ID = new Map(ROOMS.map((r) => [r.id, r]));
  const SETS_BY_ID = new Map(SETS.map((s) => [s.id, s]));

  // Application state
  const state = {
    selectedDay: "fri",
    selectedVenue: "all",
    lineup: loadLineup(),
    activeDialogSetId: null,
    activeOverlay: null, // "dialog" | "drawer" | null
    returnFocusEl: null,
    mobileMenuOpen: false,
  };

  // DOM References
  const dom = {
    pageShell: document.getElementById("page-shell"),
    dayTabs: Array.from(document.querySelectorAll(".day-tab")),
    venueChips: Array.from(document.querySelectorAll(".venue-chip")),
    schedulePanel: document.getElementById("schedule-panel"),
    scheduleSummary: document.getElementById("schedule-summary"),
    scheduleList: document.getElementById("schedule-list"),
    resetVenueBtn: document.getElementById("reset-venue-btn"),
    roomsGrid: document.getElementById("rooms-grid"),
    headerLineupBtn: document.getElementById("open-lineup-btn"),
    headerLineupCount: document.getElementById("header-lineup-count"),
    headerLineupSr: document.getElementById("header-lineup-sr"),
    heroLineupBtn: document.getElementById("hero-lineup-btn"),
    notesLineupBtn: document.getElementById("notes-lineup-btn"),
    menuToggleBtn: document.getElementById("menu-toggle-btn"),
    mobileNav: document.getElementById("mobile-nav"),
    mobileNavLinks: Array.from(document.querySelectorAll(".mobile-nav-link")),

    // Set Detail Dialog
    dialogBackdrop: document.getElementById("dialog-backdrop"),
    setDialog: document.getElementById("set-dialog"),
    dialogCloseBtn: document.getElementById("dialog-close-btn"),
    dialogKicker: document.getElementById("dialog-kicker"),
    dialogTitle: document.getElementById("dialog-artist-title"),
    dialogStyle: document.getElementById("dialog-style"),
    dialogFacts: document.getElementById("dialog-facts"),
    dialogDescription: document.getElementById("dialog-description"),
    dialogPersonnel: document.getElementById("dialog-personnel"),
    dialogRoomNote: document.getElementById("dialog-room-note"),
    dialogLineupToggle: document.getElementById("dialog-lineup-toggle"),
    dialogVenueFilterBtn: document.getElementById("dialog-venue-filter-btn"),

    // My Lineup Drawer
    drawerBackdrop: document.getElementById("drawer-backdrop"),
    lineupDrawer: document.getElementById("lineup-drawer"),
    drawerCloseBtn: document.getElementById("drawer-close-btn"),
    drawerCountPill: document.getElementById("drawer-count-pill"),
    drawerBody: document.getElementById("drawer-body"),
    drawerFooterSummary: document.getElementById("drawer-footer-summary"),
    exportIcsBtn: document.getElementById("export-ics-btn"),
    clearLineupBtn: document.getElementById("clear-lineup-btn"),

    srAnnouncer: document.getElementById("sr-announcer"),
  };

  /* --------------------------------------------------------------------------
     Persistence (localStorage)
     -------------------------------------------------------------------------- */

  function loadLineup() {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return new Set();
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return new Set();
      return new Set(parsed.filter((id) => typeof id === "string" && SETS_BY_ID.has(id)));
    } catch {
      return new Set();
    }
  }

  function persistLineup() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(Array.from(state.lineup)));
    } catch {
      // Ignore storage quota or private-mode restrictions gracefully
    }
  }

  /* --------------------------------------------------------------------------
     Calendar Export (.ics — RFC 5545, UTC from San Francisco PDT)
     -------------------------------------------------------------------------- */

  function pad2(num) {
    return String(num).padStart(2, "0");
  }

  function formatUtcIcsTimestamp(utcMs) {
    const d = new Date(utcMs);
    return (
      String(d.getUTCFullYear()) +
      pad2(d.getUTCMonth() + 1) +
      pad2(d.getUTCDate()) +
      "T" +
      pad2(d.getUTCHours()) +
      pad2(d.getUTCMinutes()) +
      pad2(d.getUTCSeconds()) +
      "Z"
    );
  }

  function escapeIcsText(value) {
    return String(value)
      .replace(/\\/g, "\\\\")
      .replace(/\r\n|\r|\n/g, "\\n")
      .replace(/;/g, "\\;")
      .replace(/,/g, "\\,");
  }

  function foldIcsLine(line) {
    const encoder = typeof TextEncoder !== "undefined" ? new TextEncoder() : null;
    const byteLength = (str) => (encoder ? encoder.encode(str).length : str.length);
    if (byteLength(line) <= 75) return line;

    const chunks = [];
    let current = "";
    let maxOctets = 75;

    for (const ch of line) {
      const next = current + ch;
      if (byteLength(next) > maxOctets && current.length > 0) {
        chunks.push(current);
        current = ch;
        maxOctets = 74; // Account for single leading space octet on continuation lines
      } else {
        current = next;
      }
    }

    if (current.length > 0) {
      chunks.push(current);
    }

    return chunks.join("\r\n ");
  }

  function buildLineupIcs(savedSets) {
    const sortedSets = [...savedSets].sort((a, b) => {
      const dayDiff = DAYS_BY_ID.get(a.day).order - DAYS_BY_ID.get(b.day).order;
      if (dayDiff !== 0) return dayDiff;
      if (a.startMin !== b.startMin) return a.startMin - b.startMin;
      return a.id.localeCompare(b.id);
    });

    const dtStamp = formatUtcIcsTimestamp(Date.now());
    const lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      "PRODID:-//After Hours SF//Festival Guide 2027//EN",
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
      "X-WR-CALNAME:After Hours 2027 — My Lineup",
    ];

    sortedSets.forEach((set) => {
      const day = DAYS_BY_ID.get(set.day);
      const room = ROOMS_BY_ID.get(set.venueId);

      // Increment date cleanly for sets ending at/after local midnight (e.g. 00:00, 00:45, 01:00)
      const effectiveEndMin =
        set.endMin <= set.startMin ? set.endMin + 24 * 60 : set.endMin;

      // Convert San Francisco local PDT (UTC-7) to exact UTC timestamps
      const startUtcMs = Date.UTC(
        day.year,
        day.month - 1,
        day.dayOfMonth,
        0,
        set.startMin + PDT_OFFSET_MINUTES,
        0
      );
      const endUtcMs = Date.UTC(
        day.year,
        day.month - 1,
        day.dayOfMonth,
        0,
        effectiveEndMin + PDT_OFFSET_MINUTES,
        0
      );

      const summary = `${set.artist} — After Hours`;
      const location = `${room.name}, ${room.address}, San Francisco, CA`;
      const description = [
        set.style,
        `Doors: ${set.doors} PDT`,
        set.description,
        `Personnel: ${set.personnel.join("; ")}`,
        set.roomNote,
      ].join("\n");

      lines.push(
        "BEGIN:VEVENT",
        `UID:after-hours-2027-${set.id}@afterhoursjazz.sf`,
        `DTSTAMP:${dtStamp}`,
        `DTSTART:${formatUtcIcsTimestamp(startUtcMs)}`,
        `DTEND:${formatUtcIcsTimestamp(endUtcMs)}`,
        `SUMMARY:${escapeIcsText(summary)}`,
        `LOCATION:${escapeIcsText(location)}`,
        `DESCRIPTION:${escapeIcsText(description)}`,
        "END:VEVENT"
      );
    });

    lines.push("END:VCALENDAR");

    return `${lines.map(foldIcsLine).join("\r\n")}\r\n`;
  }

  function exportLineupIcs() {
    const savedSets = Array.from(state.lineup)
      .map((id) => SETS_BY_ID.get(id))
      .filter(Boolean);

    if (savedSets.length === 0) return;

    const icsContent = buildLineupIcs(savedSets);
    const blob = new Blob([icsContent], { type: "text/calendar;charset=utf-8" });
    const url = URL.createObjectURL(blob);

    const link = document.createElement("a");
    link.href = url;
    link.download = "after-hours-2027-lineup.ics";
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    window.setTimeout(() => {
      URL.revokeObjectURL(url);
    }, 1000);

    const setWord = savedSets.length === 1 ? "set" : "sets";
    announce(`Exported ${savedSets.length} saved ${setWord} to after-hours-2027-lineup.ics.`);
  }

  /* --------------------------------------------------------------------------
     Screen Reader Announcer & Helpers
     -------------------------------------------------------------------------- */

  function announce(message) {
    if (!dom.srAnnouncer) return;
    dom.srAnnouncer.textContent = "";
    window.requestAnimationFrame(() => {
      dom.srAnnouncer.textContent = message;
    });
  }

  function getFocusableElements(container) {
    const selectors = [
      'a[href]:not([tabindex="-1"])',
      'button:not([disabled]):not([tabindex="-1"])',
      'input:not([disabled]):not([tabindex="-1"])',
      'select:not([disabled]):not([tabindex="-1"])',
      'textarea:not([disabled]):not([tabindex="-1"])',
      '[tabindex]:not([tabindex="-1"])',
    ].join(",");

    return Array.from(container.querySelectorAll(selectors)).filter(
      (el) => !el.hasAttribute("hidden") && el.offsetParent !== null
    );
  }

  function setBackgroundInert(isInert) {
    if (!dom.pageShell) return;
    if ("inert" in dom.pageShell) {
      dom.pageShell.inert = isInert;
    }
    if (isInert) {
      dom.pageShell.setAttribute("aria-hidden", "true");
    } else {
      dom.pageShell.removeAttribute("aria-hidden");
    }
    document.body.classList.toggle("modal-open", isInert);
  }

  function getFilteredSets() {
    return SETS.filter((set) => {
      const matchesDay = set.day === state.selectedDay;
      const matchesVenue = state.selectedVenue === "all" || set.venueId === state.selectedVenue;
      return matchesDay && matchesVenue;
    }).sort((a, b) => a.startMin - b.startMin);
  }

  function findOverlappingSetIds(daySets) {
    const overlapping = new Set();
    for (let i = 0; i < daySets.length; i += 1) {
      for (let j = i + 1; j < daySets.length; j += 1) {
        const a = daySets[i];
        const b = daySets[j];
        if (a.startMin < b.endMin && b.startMin < a.endMin) {
          overlapping.add(a.id);
          overlapping.add(b.id);
        }
      }
    }
    return overlapping;
  }

  /* --------------------------------------------------------------------------
     Rendering: Counts, Schedule, Rooms, Dialog, Drawer
     -------------------------------------------------------------------------- */

  function updateLineupCounters() {
    const count = state.lineup.size;
    const countStr = String(count);
    const srText = count === 1 ? "1 set saved" : `${count} sets saved`;

    dom.headerLineupCount.textContent = countStr;
    dom.headerLineupSr.textContent = srText;
    dom.headerLineupBtn.classList.toggle("has-items", count > 0);
    dom.drawerCountPill.textContent = countStr;
  }

  function renderDayTabs() {
    const currentDay = DAYS_BY_ID.get(state.selectedDay);
    dom.dayTabs.forEach((tab) => {
      const isSelected = tab.dataset.day === state.selectedDay;
      tab.setAttribute("aria-selected", String(isSelected));
      tab.tabIndex = isSelected ? 0 : -1;
    });
    if (currentDay) {
      dom.schedulePanel.setAttribute("aria-labelledby", currentDay.tabId);
    }
  }

  function renderVenueFilters() {
    dom.venueChips.forEach((chip) => {
      const isActive = chip.dataset.venue === state.selectedVenue;
      chip.setAttribute("aria-pressed", String(isActive));
    });
    dom.resetVenueBtn.hidden = state.selectedVenue === "all";
  }

  function renderSchedule() {
    const dayObj = DAYS_BY_ID.get(state.selectedDay);
    const venueObj = state.selectedVenue === "all" ? null : ROOMS_BY_ID.get(state.selectedVenue);
    const sets = getFilteredSets();

    const venueText = venueObj ? venueObj.name : "All Rooms";
    const setNoun = sets.length === 1 ? "set" : "sets";
    dom.scheduleSummary.textContent = `Showing ${sets.length} ${setNoun} · ${dayObj.shortLabel} · ${venueText}`;

    dom.scheduleList.replaceChildren();

    if (sets.length === 0) {
      const li = document.createElement("li");
      li.className = "empty-schedule";

      const p = document.createElement("p");
      p.textContent = `No sets scheduled in ${venueText} on ${dayObj.fullLabel}.`;

      const resetBtn = document.createElement("button");
      resetBtn.type = "button";
      resetBtn.className = "btn btn-paper-outline";
      resetBtn.textContent = "Show all rooms";
      resetBtn.addEventListener("click", () => {
        setVenueFilter("all");
      });

      li.append(p, resetBtn);
      dom.scheduleList.appendChild(li);
      return;
    }

    const fragment = document.createDocumentFragment();

    sets.forEach((set) => {
      const room = ROOMS_BY_ID.get(set.venueId);
      const isSaved = state.lineup.has(set.id);

      const li = document.createElement("li");
      li.className = isSaved ? "set-row is-saved" : "set-row";
      li.dataset.setId = set.id;

      // Time & Room Column
      const timeCol = document.createElement("div");
      timeCol.className = "set-time-col";

      const timeEl = document.createElement("span");
      timeEl.className = "set-time";
      timeEl.textContent = set.timeLabel;

      const roomBadge = document.createElement("span");
      roomBadge.className = "set-room-badge";
      roomBadge.textContent = room.name;

      timeCol.append(timeEl, roomBadge);

      if (set.late) {
        const lateTag = document.createElement("span");
        lateTag.className = "set-late-tag";
        lateTag.textContent = "Late Set";
        timeCol.appendChild(lateTag);
      }

      // Main Editorial Column
      const mainCol = document.createElement("div");
      mainCol.className = "set-main-col";

      const artistBtn = document.createElement("button");
      artistBtn.type = "button";
      artistBtn.className = "set-artist-btn";
      artistBtn.textContent = set.artist;
      artistBtn.setAttribute("aria-haspopup", "dialog");
      artistBtn.setAttribute("aria-controls", "set-dialog");
      artistBtn.addEventListener("click", () => openSetDialog(set.id, artistBtn));

      const metaLine = document.createElement("div");
      metaLine.className = "set-meta-line";
      const styleSpan = document.createElement("span");
      styleSpan.textContent = set.style;
      const hoodSpan = document.createElement("span");
      hoodSpan.textContent = `· ${room.neighborhood}`;
      metaLine.append(styleSpan, hoodSpan);

      const summaryP = document.createElement("p");
      summaryP.className = "set-summary";
      summaryP.textContent = set.summary;

      mainCol.append(artistBtn, metaLine, summaryP);

      // Actions Column
      const actionsCol = document.createElement("div");
      actionsCol.className = "set-actions-col";

      const detailBtn = document.createElement("button");
      detailBtn.type = "button";
      detailBtn.className = "set-detail-btn";
      detailBtn.textContent = "Details";
      detailBtn.setAttribute("aria-haspopup", "dialog");
      detailBtn.setAttribute("aria-controls", "set-dialog");
      detailBtn.setAttribute("aria-label", `View details for ${set.artist}`);
      detailBtn.addEventListener("click", () => openSetDialog(set.id, detailBtn));

      const lineupBtn = document.createElement("button");
      lineupBtn.type = "button";
      lineupBtn.className = "set-lineup-btn";
      lineupBtn.setAttribute("aria-pressed", String(isSaved));
      lineupBtn.setAttribute(
        "aria-label",
        isSaved ? `Remove ${set.artist} from My Lineup` : `Add ${set.artist} to My Lineup`
      );
      lineupBtn.textContent = isSaved ? "✓ In Lineup" : "+ Add";
      lineupBtn.addEventListener("click", () => toggleLineupSet(set.id));

      actionsCol.append(detailBtn, lineupBtn);

      li.append(timeCol, mainCol, actionsCol);
      fragment.appendChild(li);
    });

    dom.scheduleList.appendChild(fragment);
  }

  function renderRooms() {
    const fragment = document.createDocumentFragment();

    ROOMS.forEach((room) => {
      const roomSetCount = SETS.filter((s) => s.venueId === room.id).length;

      const card = document.createElement("article");
      card.className = "room-card";

      const top = document.createElement("div");

      const hood = document.createElement("p");
      hood.className = "room-card-num";
      hood.textContent = room.neighborhood;

      const name = document.createElement("h3");
      name.className = "room-card-name";
      name.textContent = room.name;

      const address = document.createElement("p");
      address.className = "room-card-address";
      address.textContent = room.address;

      const desc = document.createElement("p");
      desc.className = "room-card-desc";
      desc.textContent = room.description;

      const specs = document.createElement("div");
      specs.className = "room-card-specs";
      const capSpan = document.createElement("span");
      capSpan.textContent = room.capacity;
      const acSpan = document.createElement("span");
      acSpan.textContent = room.acoustics;
      const countSpan = document.createElement("span");
      countSpan.textContent = `${roomSetCount} sets`;
      specs.append(capSpan, acSpan, countSpan);

      top.append(hood, name, address, desc, specs);

      const filterBtn = document.createElement("button");
      filterBtn.type = "button";
      filterBtn.className = "room-filter-btn";
      filterBtn.textContent = `Filter schedule by ${room.name} →`;
      filterBtn.addEventListener("click", () => {
        setVenueFilter(room.id);
        const programEl = document.getElementById("program");
        if (programEl) {
          programEl.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        dom.schedulePanel.focus({ preventScroll: true });
      });

      card.append(top, filterBtn);
      fragment.appendChild(card);
    });

    dom.roomsGrid.replaceChildren(fragment);
  }

  function renderSetDialog(setId) {
    const set = SETS_BY_ID.get(setId);
    if (!set) return;

    const day = DAYS_BY_ID.get(set.day);
    const room = ROOMS_BY_ID.get(set.venueId);
    const isSaved = state.lineup.has(set.id);

    dom.dialogKicker.textContent = `${day.shortLabel} · ${set.timeLabel}`;
    dom.dialogTitle.textContent = set.artist;
    dom.dialogStyle.textContent = set.style;

    // Facts list
    dom.dialogFacts.replaceChildren();
    const facts = [
      { label: "Room", value: room.name },
      { label: "Address", value: room.address },
      { label: "Set Time", value: set.timeLabel },
      { label: "Doors Open", value: `${set.doors} · ${room.capacity}` },
    ];
    facts.forEach((fact) => {
      const wrap = document.createElement("div");
      wrap.className = "dialog-fact-item";
      const dt = document.createElement("dt");
      dt.textContent = fact.label;
      const dd = document.createElement("dd");
      dd.textContent = fact.value;
      wrap.append(dt, dd);
      dom.dialogFacts.appendChild(wrap);
    });

    dom.dialogDescription.textContent = set.description;

    dom.dialogPersonnel.replaceChildren();
    set.personnel.forEach((member) => {
      const li = document.createElement("li");
      li.textContent = member;
      dom.dialogPersonnel.appendChild(li);
    });

    dom.dialogRoomNote.textContent = set.roomNote;

    dom.dialogLineupToggle.setAttribute("aria-pressed", String(isSaved));
    dom.dialogLineupToggle.textContent = isSaved
      ? "✓ In My Lineup — Remove"
      : "+ Add to My Lineup";

    dom.dialogVenueFilterBtn.textContent = `Show ${room.name} sets`;
  }

  function renderDrawer() {
    const savedSets = Array.from(state.lineup)
      .map((id) => SETS_BY_ID.get(id))
      .filter(Boolean);

    dom.drawerBody.replaceChildren();

    if (savedSets.length === 0) {
      const emptyWrap = document.createElement("div");
      emptyWrap.className = "drawer-empty";

      const title = document.createElement("h3");
      title.className = "drawer-empty-title";
      title.textContent = "Your evening is open.";

      const text = document.createElement("p");
      text.className = "drawer-empty-text";
      text.textContent =
        "Add sets from the three-night schedule to organize your route across The Cellar, Blue Room, and Foundry Loft. Selections stay saved in this browser.";

      const browseBtn = document.createElement("button");
      browseBtn.type = "button";
      browseBtn.className = "btn btn-paper-outline";
      browseBtn.textContent = "Browse Schedule";
      browseBtn.addEventListener("click", () => {
        closeActiveOverlay();
        const programEl = document.getElementById("program");
        if (programEl) {
          programEl.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        const activeTab = dom.dayTabs.find((t) => t.dataset.day === state.selectedDay);
        if (activeTab) {
          activeTab.focus({ preventScroll: true });
        }
      });

      emptyWrap.append(title, text, browseBtn);
      dom.drawerBody.appendChild(emptyWrap);

      dom.drawerFooterSummary.textContent = "0 sets saved";
      dom.exportIcsBtn.disabled = true;
      dom.exportIcsBtn.setAttribute("aria-disabled", "true");
      dom.clearLineupBtn.hidden = true;
      return;
    }

    const fragment = document.createDocumentFragment();
    let nightsCount = 0;

    DAYS.forEach((day) => {
      const daySets = savedSets
        .filter((s) => s.day === day.id)
        .sort((a, b) => a.startMin - b.startMin);

      if (daySets.length === 0) return;
      nightsCount += 1;

      const overlappingIds = findOverlappingSetIds(daySets);

      const group = document.createElement("section");
      group.className = "drawer-day-group";
      group.setAttribute("aria-label", day.fullLabel);

      const heading = document.createElement("h3");
      heading.className = "drawer-day-heading";
      heading.textContent = `${day.shortLabel} · ${day.theme}`;

      const list = document.createElement("ul");
      list.className = "drawer-set-list";

      daySets.forEach((set) => {
        const room = ROOMS_BY_ID.get(set.venueId);

        const item = document.createElement("li");
        item.className = "drawer-set-item";

        const info = document.createElement("div");
        info.className = "drawer-set-info";

        const timeRow = document.createElement("div");
        timeRow.className = "drawer-set-time";
        const timeText = document.createElement("span");
        timeText.textContent = set.timeLabel;
        timeRow.appendChild(timeText);

        if (overlappingIds.has(set.id)) {
          const badge = document.createElement("span");
          badge.className = "overlap-badge";
          badge.textContent = "Overlap";
          timeRow.appendChild(badge);
        }

        const artistBtn = document.createElement("button");
        artistBtn.type = "button";
        artistBtn.className = "drawer-set-artist-btn";
        artistBtn.textContent = set.artist;
        artistBtn.addEventListener("click", () => {
          openSetDialog(set.id, dom.headerLineupBtn);
        });

        const roomP = document.createElement("p");
        roomP.className = "drawer-set-room";
        roomP.textContent = `${room.name} · ${room.neighborhood}`;

        info.append(timeRow, artistBtn, roomP);

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "drawer-remove-btn";
        removeBtn.dataset.removeId = set.id;
        removeBtn.textContent = "Remove";
        removeBtn.setAttribute("aria-label", `Remove ${set.artist} from My Lineup`);
        removeBtn.addEventListener("click", () => removeFromDrawer(set.id));

        item.append(info, removeBtn);
        list.appendChild(item);
      });

      group.append(heading, list);
      fragment.appendChild(group);
    });

    dom.drawerBody.appendChild(fragment);

    const setWord = savedSets.length === 1 ? "set" : "sets";
    const nightWord = nightsCount === 1 ? "night" : "nights";
    dom.drawerFooterSummary.textContent = `${savedSets.length} ${setWord} · ${nightsCount} ${nightWord}`;
    dom.exportIcsBtn.disabled = false;
    dom.exportIcsBtn.removeAttribute("aria-disabled");
    dom.clearLineupBtn.hidden = false;
  }

  /* --------------------------------------------------------------------------
     State Mutations
     -------------------------------------------------------------------------- */

  function selectDay(dayId) {
    if (!DAYS_BY_ID.has(dayId) || state.selectedDay === dayId) return;
    state.selectedDay = dayId;
    renderDayTabs();
    renderSchedule();
  }

  function setVenueFilter(venueId) {
    if (venueId !== "all" && !ROOMS_BY_ID.has(venueId)) return;
    state.selectedVenue = venueId;
    renderVenueFilters();
    renderSchedule();
  }

  function toggleLineupSet(setId) {
    const set = SETS_BY_ID.get(setId);
    if (!set) return;

    const wasSaved = state.lineup.has(setId);
    if (wasSaved) {
      state.lineup.delete(setId);
      announce(`Removed ${set.artist} from My Lineup. ${state.lineup.size} saved.`);
    } else {
      state.lineup.add(setId);
      announce(`Added ${set.artist} to My Lineup. ${state.lineup.size} saved.`);
    }

    persistLineup();
    updateLineupCounters();

    // Update schedule button in place when present so keyboard focus stays undisturbed
    const row = dom.scheduleList.querySelector(`.set-row[data-set-id="${setId}"]`);
    if (row) {
      const nowSaved = !wasSaved;
      row.classList.toggle("is-saved", nowSaved);
      const btn = row.querySelector(".set-lineup-btn");
      if (btn) {
        btn.setAttribute("aria-pressed", String(nowSaved));
        btn.setAttribute(
          "aria-label",
          nowSaved ? `Remove ${set.artist} from My Lineup` : `Add ${set.artist} to My Lineup`
        );
        btn.textContent = nowSaved ? "✓ In Lineup" : "+ Add";
      }
    }

    if (state.activeOverlay === "dialog" && state.activeDialogSetId === setId) {
      renderSetDialog(setId);
    }

    if (state.activeOverlay === "drawer") {
      renderDrawer();
    }
  }

  function removeFromDrawer(setId) {
    const set = SETS_BY_ID.get(setId);
    if (!set) return;

    const currentButtons = Array.from(dom.drawerBody.querySelectorAll(".drawer-remove-btn"));
    const currentIndex = currentButtons.findIndex((b) => b.dataset.removeId === setId);

    state.lineup.delete(setId);
    persistLineup();
    updateLineupCounters();
    renderSchedule();
    renderDrawer();

    announce(`Removed ${set.artist} from My Lineup. ${state.lineup.size} saved.`);

    const remainingButtons = Array.from(dom.drawerBody.querySelectorAll(".drawer-remove-btn"));
    if (remainingButtons.length > 0) {
      const nextIndex = Math.min(currentIndex, remainingButtons.length - 1);
      remainingButtons[Math.max(0, nextIndex)].focus();
    } else {
      dom.drawerCloseBtn.focus();
    }
  }

  function clearLineup() {
    if (state.lineup.size === 0) return;
    state.lineup.clear();
    persistLineup();
    updateLineupCounters();
    renderSchedule();
    renderDrawer();
    announce("Cleared all sets from My Lineup.");
    dom.drawerCloseBtn.focus();
  }

  /* --------------------------------------------------------------------------
     Overlays: Set Detail Dialog & My Lineup Drawer
     -------------------------------------------------------------------------- */

  function openSetDialog(setId, triggerEl) {
    if (!SETS_BY_ID.has(setId)) return;

    // Close drawer cleanly if opening a set directly from My Lineup
    if (state.activeOverlay === "drawer") {
      dom.drawerBackdrop.hidden = true;
      dom.headerLineupBtn.setAttribute("aria-expanded", "false");
    }

    closeMobileMenu();

    state.activeDialogSetId = setId;
    state.activeOverlay = "dialog";
    state.returnFocusEl = triggerEl || document.activeElement;

    renderSetDialog(setId);
    dom.dialogBackdrop.hidden = false;
    setBackgroundInert(true);

    dom.dialogCloseBtn.focus();
  }

  function openLineupDrawer(triggerEl) {
    if (state.activeOverlay === "dialog") {
      dom.dialogBackdrop.hidden = true;
      state.activeDialogSetId = null;
    }

    closeMobileMenu();

    state.activeOverlay = "drawer";
    state.returnFocusEl = triggerEl || document.activeElement;

    renderDrawer();
    dom.drawerBackdrop.hidden = false;
    dom.headerLineupBtn.setAttribute("aria-expanded", "true");
    setBackgroundInert(true);

    dom.drawerCloseBtn.focus();
  }

  function closeActiveOverlay() {
    if (!state.activeOverlay) return;

    if (state.activeOverlay === "dialog") {
      dom.dialogBackdrop.hidden = true;
      state.activeDialogSetId = null;
    } else if (state.activeOverlay === "drawer") {
      dom.drawerBackdrop.hidden = true;
      dom.headerLineupBtn.setAttribute("aria-expanded", "false");
    }

    state.activeOverlay = null;
    setBackgroundInert(false);

    const target = state.returnFocusEl;
    state.returnFocusEl = null;

    if (target && target.isConnected && target.offsetParent !== null) {
      target.focus();
    } else {
      dom.headerLineupBtn.focus();
    }
  }

  function handleOverlayKeydown(event) {
    if (!state.activeOverlay) {
      if (event.key === "Escape" && state.mobileMenuOpen) {
        closeMobileMenu(true);
      }
      return;
    }

    if (event.key === "Escape") {
      event.preventDefault();
      closeActiveOverlay();
      return;
    }

    if (event.key === "Tab") {
      const container =
        state.activeOverlay === "dialog" ? dom.setDialog : dom.lineupDrawer;
      const focusables = getFocusableElements(container);
      if (focusables.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }

      const first = focusables[0];
      const last = focusables[focusables.length - 1];

      if (event.shiftKey) {
        if (document.activeElement === first || !container.contains(document.activeElement)) {
          event.preventDefault();
          last.focus();
        }
      } else if (
        document.activeElement === last ||
        !container.contains(document.activeElement)
      ) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  /* --------------------------------------------------------------------------
     Mobile Navigation Menu
     -------------------------------------------------------------------------- */

  function toggleMobileMenu() {
    if (state.mobileMenuOpen) {
      closeMobileMenu(false);
    } else {
      state.mobileMenuOpen = true;
      dom.mobileNav.hidden = false;
      dom.menuToggleBtn.setAttribute("aria-expanded", "true");
      dom.menuToggleBtn.setAttribute("aria-label", "Close navigation menu");
    }
  }

  function closeMobileMenu(restoreFocus = false) {
    if (!state.mobileMenuOpen) return;
    state.mobileMenuOpen = false;
    dom.mobileNav.hidden = true;
    dom.menuToggleBtn.setAttribute("aria-expanded", "false");
    dom.menuToggleBtn.setAttribute("aria-label", "Open navigation menu");
    if (restoreFocus) {
      dom.menuToggleBtn.focus();
    }
  }

  /* --------------------------------------------------------------------------
     Event Wiring
     -------------------------------------------------------------------------- */

  function bindEvents() {
    // Day tabs (click + WAI-ARIA arrow/Home/End keyboard navigation)
    dom.dayTabs.forEach((tab, index) => {
      tab.addEventListener("click", () => {
        selectDay(tab.dataset.day);
      });

      tab.addEventListener("keydown", (event) => {
        let nextIndex = null;
        if (event.key === "ArrowRight") {
          nextIndex = (index + 1) % dom.dayTabs.length;
        } else if (event.key === "ArrowLeft") {
          nextIndex = (index - 1 + dom.dayTabs.length) % dom.dayTabs.length;
        } else if (event.key === "Home") {
          nextIndex = 0;
        } else if (event.key === "End") {
          nextIndex = dom.dayTabs.length - 1;
        }

        if (nextIndex !== null) {
          event.preventDefault();
          const nextTab = dom.dayTabs[nextIndex];
          selectDay(nextTab.dataset.day);
          nextTab.focus();
        }
      });
    });

    // Local venue filters
    dom.venueChips.forEach((chip) => {
      chip.addEventListener("click", () => {
        setVenueFilter(chip.dataset.venue);
      });
    });

    dom.resetVenueBtn.addEventListener("click", () => {
      setVenueFilter("all");
    });

    // Lineup drawer triggers
    dom.headerLineupBtn.addEventListener("click", () =>
      openLineupDrawer(dom.headerLineupBtn)
    );
    dom.heroLineupBtn.addEventListener("click", () => openLineupDrawer(dom.heroLineupBtn));
    dom.notesLineupBtn.addEventListener("click", () =>
      openLineupDrawer(dom.notesLineupBtn)
    );

    // Dialog controls
    dom.dialogCloseBtn.addEventListener("click", closeActiveOverlay);
    dom.dialogBackdrop.addEventListener("click", (event) => {
      if (event.target === dom.dialogBackdrop) {
        closeActiveOverlay();
      }
    });

    dom.dialogLineupToggle.addEventListener("click", () => {
      if (state.activeDialogSetId) {
        toggleLineupSet(state.activeDialogSetId);
      }
    });

    dom.dialogVenueFilterBtn.addEventListener("click", () => {
      const set = SETS_BY_ID.get(state.activeDialogSetId);
      if (!set) return;
      const venueId = set.venueId;
      closeActiveOverlay();
      setVenueFilter(venueId);
      const programEl = document.getElementById("program");
      if (programEl) {
        programEl.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      dom.schedulePanel.focus({ preventScroll: true });
    });

    // Drawer controls
    dom.drawerCloseBtn.addEventListener("click", closeActiveOverlay);
    dom.drawerBackdrop.addEventListener("click", (event) => {
      if (event.target === dom.drawerBackdrop) {
        closeActiveOverlay();
      }
    });
    dom.exportIcsBtn.addEventListener("click", exportLineupIcs);
    dom.clearLineupBtn.addEventListener("click", clearLineup);

    // Mobile menu
    dom.menuToggleBtn.addEventListener("click", toggleMobileMenu);
    dom.mobileNavLinks.forEach((link) => {
      link.addEventListener("click", () => closeMobileMenu(false));
    });

    window.addEventListener("resize", () => {
      if (window.innerWidth > 720 && state.mobileMenuOpen) {
        closeMobileMenu(false);
      }
    });

    // Global keyboard handling (Escape + focus trap)
    document.addEventListener("keydown", handleOverlayKeydown);
  }

  /* --------------------------------------------------------------------------
     Initialize
     -------------------------------------------------------------------------- */

  function init() {
    renderDayTabs();
    renderVenueFilters();
    renderSchedule();
    renderRooms();
    updateLineupCounters();
    bindEvents();
  }

  init();
})();
