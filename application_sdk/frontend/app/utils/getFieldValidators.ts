import Schema from 'async-validator'

export function getFieldValidators(property: Record<string, unknown>) {
    const schema = new Schema({
        [property.id]: property.ui?.rules ?? [],
    })

    return {
        onBlurAsync: async ({ value }) => {
            try {
                await schema.validate({ [property.id]: value })
            } catch ({ errors, fields }) {
                return errors[0].message
            }

            return undefined
        },
    }
}
