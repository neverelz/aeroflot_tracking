import unittest

from itinerary import FlightOffer, cheapest_matching_offer, offer_matches, parse_offer_text


class ItineraryTests(unittest.TestCase):
    def test_parse_direct_offer(self):
        offer = parse_offer_text(
            "SVO 10:20 SCO 15:10 Без пересадок В пути 3 ч 50 мин от 18 450 ₽"
        )
        self.assertIsNotNone(offer)
        self.assertEqual(offer.price, 18450)
        self.assertEqual(offer.stops, 0)
        self.assertEqual(offer.total_duration_minutes, 230)
        self.assertEqual(offer.route, ("SVO", "SCO"))

    def test_parse_connection_offer(self):
        offer = parse_offer_text(
            "SVO 08:00 ALA 12:00 Пересадка в ALA 2 ч 15 мин "
            "ALA 14:15 SCO 16:00 1 пересадка В пути 8 ч от 21 900 ₽"
        )
        self.assertIsNotNone(offer)
        self.assertEqual(offer.stops, 1)
        self.assertEqual(offer.layovers, (135,))
        self.assertEqual(offer.route, ("SVO", "ALA", "SCO"))

    def test_infers_connection_from_hidden_segment_times(self):
        offer = parse_offer_text(
            "SVO 08:00 ALA 11:00 ALA 13:00 SCO 15:00 В пути 7 ч от 20 000 ₽"
        )
        self.assertIsNotNone(offer)
        self.assertEqual(offer.stops, 1)
        self.assertEqual(offer.layovers, (120,))

    def test_does_not_call_collapsed_unknown_route_direct(self):
        offer = parse_offer_text("SVO 08:00 SCO 15:00 В пути 7 ч от 20 000 ₽")
        self.assertIsNone(offer)

    def test_filter_uses_max_stops_and_connection_window(self):
        task = {
            "max_stops": 1,
            "min_connection_minutes": 90,
            "max_connection_minutes": 300,
            "avoid_overnight": True,
            "allow_airport_change": False,
        }
        direct = FlightOffer(price=25000, stops=0)
        comfortable = FlightOffer(price=22000, stops=1, layovers=(120,))
        too_short = FlightOffer(price=18000, stops=1, layovers=(40,))
        two_stops = FlightOffer(price=17000, stops=2, layovers=(120, 120))

        self.assertTrue(offer_matches(task, direct))
        self.assertTrue(offer_matches(task, comfortable))
        self.assertFalse(offer_matches(task, too_short))
        self.assertFalse(offer_matches(task, two_stops))
        self.assertEqual(
            cheapest_matching_offer(task, [direct, comfortable, too_short, two_stops]),
            comfortable,
        )

    def test_rejects_incomplete_or_inconvenient_connection(self):
        task = {
            "max_stops": 2,
            "min_connection_minutes": 60,
            "max_connection_minutes": 300,
            "avoid_overnight": True,
            "allow_airport_change": False,
        }
        self.assertFalse(offer_matches(task, FlightOffer(10000, 1)))
        self.assertFalse(offer_matches(task, FlightOffer(10000, 1, (120,), overnight=True)))
        self.assertFalse(offer_matches(task, FlightOffer(10000, 1, (120,), airport_change=True)))

    def test_detects_overnight_and_airport_change(self):
        overnight = parse_offer_text(
            "SVO 20:00 ALA 23:30 Пересадка 2 ч ALA 01:30 SCO 03:00 "
            "1 пересадка В пути 7 ч 18 000 ₽"
        )
        airport_change = parse_offer_text(
            "SVO 08:00 VKO 10:00 Стыковка 2 ч SVO 12:00 SCO 15:00 "
            "1 пересадка В пути 7 ч 19 000 ₽"
        )
        self.assertTrue(overnight.overnight)
        self.assertTrue(airport_change.airport_change)


if __name__ == "__main__":
    unittest.main()
