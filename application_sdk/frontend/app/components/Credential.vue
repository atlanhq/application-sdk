<script setup lang="ts">
    // libraries
    import { Button } from '@atlanhq/atlantis'
    import { useForm } from '@tanstack/vue-form'
    import { FormInjectionKey } from '~/constants/workflows'
    import { buildCredentialBody } from '~/utils/buildCredentialBody'
    import { crush, omit } from 'radash'

    const FormItem = defineAsyncComponent(
        () => import('~/components/DynamicForm.vue')
    )

    const { property } = defineProps<{
        property: Record<string, unknown>
        modelValue: string | undefined
    }>()

    const form = inject(FormInjectionKey) as ReturnType<typeof useForm>

    const { data } = await useAsyncData(
        () => `credential_${property.ui.credentialType}`,
        () =>
            $fetch(
                `http://localhost:8000/workflows/v1/configmap/${property.ui.credentialType}`
            )
    )

    const formState = form.useStore((state) =>
        omit(state.values, ['credential_guid_body'])
    )

    watch(
        formState,
        (updatedFormState) => {
            if (updatedFormState) {
                form.setFieldValue(
                    'credential_guid_body',
                    buildCredentialBody(
                        crush(updatedFormState),
                        property.id,
                        property.ui?.credentialType,
                        undefined
                    )
                )
            }
        },
        {
            immediate: true,
        }
    )

    watch(
        data,
        (updatedData) => {
            Object.entries(
                getDefaultValuesFromConfigMap(updatedData.data.config)
            ).forEach(([key, value]) => {
                if (!form.state.values[`${property.id}.${key}`]) {
                    form.setFieldValue(`${property.id}.${key}`, value)
                }
            })
        },
        {
            immediate: true,
        }
    )

    const isTesting = ref(false)
    const testError = ref(null)

    async function handleTestAuthentication() {
        isTesting.value = true

        try {
            await $fetch('http://localhost:8000/workflows/v1/auth', {
                method: 'POST',
                body: JSON.stringify(
                    buildCredentialBody(
                        crush(form.state.values),
                        property.id,
                        property.ui?.credentialType,
                        undefined
                    )
                ),
            })
        } catch (err) {
            testError.value = err
        } finally {
            isTesting.value = false
        }
    }
</script>

<template>
    <div :class="property.ui.class">
        <!-- Setup layout -->
        <template v-if="data?.data?.config">
            <FormItem :configMap="data.data.config" :baseKey="property.id" />
            <div class="flex mt-3">
                <Button
                    :loading="isTesting"
                    :label="
                        isTesting
                            ? 'Testing Authentication'
                            : 'Test Authentication'
                    "
                    @click="handleTestAuthentication"
                />
            </div>
        </template>

        <div
            v-if="testError"
            class="p-3 mt-4 border rounded-md border-new-red-400 bg-new-red-100"
        >
            <span class="text-sm font-semibold text-new-gray-700">
                Error log
            </span>
            <div class="overflow-auto max-h-48">
                <span
                    class="font-mono text-xs whitespace-pre-line text-new-gray-600"
                    data-test-id="error-log-message"
                >
                    {{ testError }}
                </span>
            </div>
        </div>
    </div>
</template>
