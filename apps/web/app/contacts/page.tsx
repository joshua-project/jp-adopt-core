import { isB2cClientConfigured } from "../../src/lib/b2c/msalConfig";
import { ContactsB2C } from "../../src/components/ContactsB2C";
import { ContactsDevOnly } from "../../src/components/ContactsDevOnly";

export default function ContactsPage() {
  if (isB2cClientConfigured()) {
    return <ContactsB2C />;
  }
  return <ContactsDevOnly />;
}
