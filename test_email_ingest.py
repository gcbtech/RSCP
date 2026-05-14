
import unittest
from email_ingest import extract_order_id, parse_amazon_items, parse_ebay_items
from bs4 import BeautifulSoup

class TestEmailIngest(unittest.TestCase):
    def test_extract_order_id_amazon(self):
        text = "Your order # 113-1234567-1234567 has shipped"
        oid, src = extract_order_id(text, "")
        self.assertEqual(oid, "113-1234567-1234567")
        self.assertEqual(src, "Amazon")

    def test_extract_order_id_ebay(self):
        text = "Order update for 26-14075-32104"
        oid, src = extract_order_id(text, "")
        self.assertEqual(oid, "26-14075-32104")
        self.assertEqual(src, "eBay")

    def test_parse_amazon_multi_item(self):
        html = """
        <html>
        <body>
            <table>
                <tr>
                    <td><a href="https://www.amazon.com/dp/B08XYZ1234"><img src="https://images-na.ssl-images-amazon.com/images/I/51TestImage.jpg" alt="Test Item 1"></a></td>
                    <td><a href="https://www.amazon.com/dp/B08XYZ1234">Test Item 1</a></td>
                </tr>
                <tr>
                     <td><a href="https://www.amazon.com/gp/product/B09ABC5678"><img src="https://images-na.ssl-images-amazon.com/images/I/51TestImage2.jpg" alt="Test Item 2"></a></td>
                    <td><a href="https://www.amazon.com/gp/product/B09ABC5678">Test Item 2</a></td>
                </tr>
            </table>
        </body>
        </html>
        """
        soup = BeautifulSoup(html, 'html.parser')
        items = parse_amazon_items(soup)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['name'], "Test Item 1")
        self.assertEqual(items[0]['asin'], "B08XYZ1234")
        self.assertEqual(items[0]['name'], "Test Item 1")
        self.assertEqual(items[0]['asin'], "B08XYZ1234")
        self.assertEqual(items[1]['name'], "Test Item 2")
        self.assertEqual(items[1]['asin'], "B09ABC5678")
        
    def test_parse_amazon_qty(self):
        html = """
        <div>
            <a href="/dp/B123456789">Item Name</a>
            Qty: 3
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        items = parse_amazon_items(soup)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['quantity'], 3)

    def test_parse_amazon_qty_with_space(self):
        html = """
        <div>
            <a href="/dp/B987654321">Item Name</a>
            Qty : 5
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        items = parse_amazon_items(soup)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['quantity'], 5)

    def test_using_qty_heuristic(self):
        from email_ingest import parse_using_qty_heuristic
        html = """
        <table>
            <tr>
                <td><img src="https://m.media-amazon.com/images/I/71xyz.jpg"></td>
                <td>
                    <b>Target Item Name</b>
                    <div>Qty : 3</div>
                </td>
            </tr>
        </table>
        """
        soup = BeautifulSoup(html, 'html.parser')
        items = parse_using_qty_heuristic(soup)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], "Target Item Name")
        self.assertEqual(items[0]['quantity'], 3)
        self.assertEqual(items[0]['image_url'], "https://m.media-amazon.com/images/I/71xyz.jpg")

    def test_parse_ebay_items(self):
        html = """
        <html>
        <body>
            <table>
                <tr>
                    <td><img src="https://i.ebayimg.com/images/g/test/s-l500.jpg" alt="eBay Item Title"></td>
                </tr>
            </table>
        </body>
        </html>
        """
        soup = BeautifulSoup(html, 'html.parser')
        items = parse_ebay_items(soup)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], "eBay Item Title")
        self.assertEqual(items[0]['image_url'], "https://i.ebayimg.com/images/g/test/s-l500.jpg")

if __name__ == '__main__':
    unittest.main()
