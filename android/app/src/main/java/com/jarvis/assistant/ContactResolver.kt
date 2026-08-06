package com.jarvis.assistant

import android.content.Context
import android.provider.ContactsContract

/**
 * Turning "mum" into a phone number.
 *
 * This is the single most dangerous lookup in the app, and it is worth being
 * clear about why. Everything downstream is irreversible — a sent text cannot
 * be unsent, a placed call cannot be unplaced — and the input is a name
 * produced by a speech recogniser from a spoken word, which is exactly the
 * kind of input that arrives subtly wrong. "Mum" and "Tom" are one phoneme
 * apart.
 *
 * So the rule throughout: when the answer is not obvious, return Ambiguous and
 * make the human choose. A wrong number sent confidently is far worse than a
 * question asked unnecessarily.
 */
class ContactResolver(private val context: Context) {

    data class Contact(val name: String, val number: String)

    sealed class Resolution {
        data class Found(val contact: Contact) : Resolution()
        data class Ambiguous(val candidates: List<Contact>) : Resolution()
        object NotFound : Resolution()
        data class Denied(val reason: String) : Resolution()
    }

    /**
     * Look up [query] among the user's contacts.
     *
     * Matching runs tightest-first — exact, then prefix, then substring — and
     * stops at the first tier that produces results. Falling through to looser
     * matching only when stricter matching found nothing keeps "Sam" from
     * dragging in "Samantha" whenever a real Sam exists.
     */
    fun resolve(query: String): Resolution {
        val needle = query.trim()
        if (needle.isEmpty()) return Resolution.NotFound

        val all = try {
            loadContacts()
        } catch (e: SecurityException) {
            return Resolution.Denied("I don't have permission to read your contacts.")
        }

        if (all.isEmpty()) return Resolution.NotFound

        val exact = all.filter { it.name.equals(needle, ignoreCase = true) }
        if (exact.isNotEmpty()) return disambiguate(exact)

        // A spoken first name is the overwhelmingly common case, so a match on
        // the first word counts as strongly as a match on the whole name.
        val firstName = all.filter {
            it.name.substringBefore(' ').equals(needle, ignoreCase = true)
        }
        if (firstName.isNotEmpty()) return disambiguate(firstName)

        val prefix = all.filter { it.name.startsWith(needle, ignoreCase = true) }
        if (prefix.isNotEmpty()) return disambiguate(prefix)

        val contains = all.filter { it.name.contains(needle, ignoreCase = true) }
        if (contains.isNotEmpty()) return disambiguate(contains)

        return Resolution.NotFound
    }

    private fun disambiguate(matches: List<Contact>): Resolution {
        // Several stored numbers for one person is not ambiguity worth asking
        // about — it is one human with a mobile and a landline.
        val distinctPeople = matches.distinctBy { it.name.lowercase() }

        if (distinctPeople.size == 1) {
            return Resolution.Found(matches.first())
        }

        // Two different people. Never pick one. This is precisely the moment
        // the message goes to the wrong person.
        return Resolution.Ambiguous(distinctPeople.take(5))
    }

    private fun loadContacts(): List<Contact> {
        val contacts = mutableListOf<Contact>()

        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER,
        )

        context.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            projection,
            null,
            null,
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " ASC",
        )?.use { cursor ->
            val nameColumn = cursor.getColumnIndex(
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME
            )
            val numberColumn = cursor.getColumnIndex(
                ContactsContract.CommonDataKinds.Phone.NUMBER
            )

            while (cursor.moveToNext()) {
                val name = cursor.getString(nameColumn)?.trim().orEmpty()
                val number = cursor.getString(numberColumn)?.trim().orEmpty()
                if (name.isNotEmpty() && number.isNotEmpty()) {
                    contacts += Contact(name, number)
                }
            }
        }

        return contacts
    }
}
